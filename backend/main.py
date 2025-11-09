from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import json
import pandas as pd
import sqlite3
from io import BytesIO
import os
from pydantic import BaseModel
from typing import Dict, Optional, List

DB_PATH = "data/orders.db"
TABLE_NAME = "orders"

ALLOWED_ORDER_TYPES = {
    "Deployment",
    "Procurement to Recipient",
    "Retrieval",
    "Drop Retrieval",
}

REQUIRED_UPLOAD_COLUMNS = {"Order Number", "Order Type", "Organization", "Created At"}

TIMESTAMP_COLUMN_MAP = {
    "Created At": "created_at",
    "In Fulfillment Date": "in_fulfillment_at",
    "Shipped Date": "shipped_at",
    "In Return Date": "in_return_at",
    "Pending Return Date": "pending_return_at",
    "Completed Date": "completed_at",
}

STRING_COLUMN_MAP = {
    "Order Number": "order_id",
    "Order Type": "order_type",
    "Organization": "customer_name",
    "Status": "status",
    "Shipping Type": "shipping_type",
}

INTEGER_COLUMN_MAP = {
    "Row Count": "row_count",
    "Items": "items",
}

UPSERT_COLUMNS = [
    "order_id",
    "order_type",
    "customer_name",
    "status",
    "shipping_type",
    "row_count",
    "created_at",
    "in_fulfillment_at",
    "shipped_at",
    "in_return_at",
    "pending_return_at",
    "completed_at",
    "items",
    "raw_json",
]

STAGE_PAIRS = [
    ("created_at", "in_fulfillment_at", "created_to_in_fulfillment"),
    ("in_fulfillment_at", "shipped_at", "in_fulfillment_to_shipped"),
    ("shipped_at", "in_return_at", "shipped_to_in_return"),
    ("in_return_at", "pending_return_at", "in_return_to_pending_return"),
    ("pending_return_at", "completed_at", "pending_return_to_completed"),
    ("shipped_at", "completed_at", "shipped_to_completed"),
    ("created_at", "completed_at", "created_to_completed"),
]

app = FastAPI(title="3PL Performance API")

# Allow localhost frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def ensure_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            order_id TEXT PRIMARY KEY,
            order_type TEXT,
            customer_name TEXT,
            status TEXT,
            shipping_type TEXT,
            row_count INTEGER,
            created_at TEXT,
            in_fulfillment_at TEXT,
            shipped_at TEXT,
            in_return_at TEXT,
            pending_return_at TEXT,
            completed_at TEXT,
            items INTEGER,
            raw_json TEXT
        )
        """
    )
    cursor.execute(f"PRAGMA table_info({TABLE_NAME})")
    existing_columns = {row[1] for row in cursor.fetchall()}
    required_column_defs: Dict[str, str] = {
        "order_type": "TEXT",
        "customer_name": "TEXT",
        "shipping_type": "TEXT",
        "row_count": "INTEGER",
        "in_fulfillment_at": "TEXT",
        "in_return_at": "TEXT",
        "pending_return_at": "TEXT",
        "items": "INTEGER",
        "raw_json": "TEXT",
    }
    for column, column_type in required_column_defs.items():
        if column not in existing_columns:
            cursor.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} {column_type}"
            )
    conn.commit()
    conn.close()

ensure_db()

def _clean_string(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _parse_int(value: Optional[str]) -> Optional[int]:
    text = _clean_string(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_timestamp(value: Optional[str]) -> Optional[str]:
    text = _clean_string(value)
    if text is None:
        return None
    timestamp = pd.to_datetime(text, errors="coerce")
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


def _row_to_raw_json(row: pd.Series) -> str:
    payload = {}
    for key, value in row.items():
        if pd.isna(value):
            payload[key] = None
        else:
            payload[key] = value
    return json.dumps(payload, default=str)


def _normalize_upload(df: pd.DataFrame):
    stats = {
        "total_rows": int(df.shape[0]),
        "skipped_wrong_order_type": 0,
        "skipped_missing_required": 0,
        "deduplicated": 0,
    }

    df = df.copy()
    df.columns = [col.strip() for col in df.columns]

    missing_columns = REQUIRED_UPLOAD_COLUMNS - set(df.columns)
    if missing_columns:
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns: {', '.join(sorted(missing_columns))}",
        )

    allowed_mask = df["Order Type"].isin(ALLOWED_ORDER_TYPES)
    stats["skipped_wrong_order_type"] = int((~allowed_mask).sum())
    df = df[allowed_mask].copy()

    records_map: Dict[str, Dict[str, Optional[str]]] = {}
    for _, row in df.iterrows():
        order_id = _clean_string(row.get("Order Number"))
        created_at = _parse_timestamp(row.get("Created At"))
        if not order_id or created_at is None:
            stats["skipped_missing_required"] += 1
            continue

        record: Dict[str, Optional[str]] = {column: None for column in UPSERT_COLUMNS}
        record["order_id"] = order_id
        record["created_at"] = created_at

        for csv_column, target in STRING_COLUMN_MAP.items():
            if csv_column == "Order Number":
                continue
            record[target] = _clean_string(row.get(csv_column))

        for csv_column, target in TIMESTAMP_COLUMN_MAP.items():
            if csv_column == "Created At":
                continue
            record[target] = _parse_timestamp(row.get(csv_column))

        for csv_column, target in INTEGER_COLUMN_MAP.items():
            record[target] = _parse_int(row.get(csv_column))

        record["raw_json"] = _row_to_raw_json(row)

        records_map[order_id] = record

    stats["deduplicated"] = len(df) - len(records_map)
    records = [records_map[order_id] for order_id in records_map]
    return records, stats


@app.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    """
    Accept a CSV export of orders, normalize it to the analytics schema, and upsert rows.
    Only allowed order types are ingested.
    """
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    contents = await file.read()
    try:
        df = pd.read_csv(BytesIO(contents), dtype=str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {exc}")

    records, stats = _normalize_upload(df)
    if not records:
        return {"imported": 0, **stats, "allowed_order_types": sorted(ALLOWED_ORDER_TYPES)}

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    placeholders = ", ".join("?" for _ in UPSERT_COLUMNS)
    upsert_sql = f"""
        INSERT INTO {TABLE_NAME} ({', '.join(UPSERT_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(order_id) DO UPDATE SET
            {', '.join(f"{column}=excluded.{column}" for column in UPSERT_COLUMNS if column != "order_id")}
    """
    rows = [tuple(record[column] for column in UPSERT_COLUMNS) for record in records]
    cursor.executemany(upsert_sql, rows)
    conn.commit()
    conn.close()

    return {"imported": len(rows), **stats, "allowed_order_types": sorted(ALLOWED_ORDER_TYPES)}

def load_orders(start: Optional[str] = None, end: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(f"SELECT * FROM {TABLE_NAME}", conn)
    conn.close()
    if df.empty:
        return df

    for column in [
        "created_at",
        "in_fulfillment_at",
        "shipped_at",
        "in_return_at",
        "pending_return_at",
        "completed_at",
    ]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")

    for column in ["row_count", "items"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    if start:
        start_ts = pd.to_datetime(start, utc=True, errors="coerce")
        if pd.isna(start_ts):
            raise HTTPException(status_code=400, detail="Invalid start timestamp")
        df = df[df["created_at"] >= start_ts]
    if end:
        end_ts = pd.to_datetime(end, utc=True, errors="coerce")
        if pd.isna(end_ts):
            raise HTTPException(status_code=400, detail="Invalid end timestamp")
        df = df[df["created_at"] <= end_ts]
    return df

class MetricsParams(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    aged_threshold_days: Optional[int] = Query(3)

def _compute_stage_durations(frame: pd.DataFrame) -> Dict[str, Dict[str, Optional[float]]]:
    stage_metrics: Dict[str, Dict[str, Optional[float]]] = {}
    for start_column, end_column, metric_key in STAGE_PAIRS:
        if start_column not in frame.columns or end_column not in frame.columns:
            continue
        subset = frame[[start_column, end_column]].dropna()
        if subset.empty:
            continue
        durations = (
            (subset[end_column] - subset[start_column]).dt.total_seconds() / 3600
        )
        durations = durations[durations >= 0]
        if durations.empty:
            continue
        stage_metrics[metric_key] = {
            "count": int(durations.count()),
            "mean_hours": float(durations.mean()),
            "median_hours": float(durations.median()),
            "p90_hours": float(durations.quantile(0.9)),
        }
    return stage_metrics


@app.get("/metrics")
def metrics(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    aged_threshold_days: int = Query(3),
):
    """
    Compute dashboard metrics aggregated across the filtered date range.
    """
    df = load_orders(start, end)
    if df.empty:
        return {
            "counts": {},
            "kpis": {},
            "aged_orders": [],
            "timeseries": {},
        }

    total_orders = int(len(df))
    completed_orders = int(df["completed_at"].notna().sum())
    completion_rate = completed_orders / total_orders if total_orders else 0
    backlog_df = df[df["completed_at"].isna()].copy()
    backlog_count = int(len(backlog_df))

    durations = _compute_stage_durations(df)
    durations_by_order_type = {}
    for order_type, grouped_df in df.groupby("order_type"):
        durations_by_order_type[order_type] = _compute_stage_durations(grouped_df)

    threshold_timestamp = pd.Timestamp.utcnow() - pd.Timedelta(days=aged_threshold_days)
    aged_candidates = backlog_df[
        backlog_df["created_at"].notna()
        & (backlog_df["created_at"] < threshold_timestamp)
    ]
    aged_orders = []
    for _, row in aged_candidates.sort_values("created_at").iterrows():
        created_at = row["created_at"]
        age_days = (
            int((pd.Timestamp.utcnow() - created_at).total_seconds() // 86400)
            if pd.notna(created_at)
            else None
        )
        aged_orders.append(
            {
                "order_id": row["order_id"],
                "customer_name": row.get("customer_name"),
                "order_type": row.get("order_type"),
                "status": row.get("status"),
                "created_at": created_at.isoformat() if pd.notna(created_at) else None,
                "age_days": age_days,
            }
        )

    timeseries: Dict[str, List[Dict[str, object]]] = {"throughput": []}
    if df["completed_at"].notna().any():
        completed_df = df[df["completed_at"].notna()].copy()
        completed_df["date"] = completed_df["completed_at"].dt.date
        throughput = (
            completed_df.groupby(["date", "order_type"])
            .size()
            .reset_index(name="completed")
        )
        timeseries["throughput"] = throughput.to_dict(orient="records")

    status_counts = (
        df.groupby(df["status"].fillna("unknown")).size().to_dict()
        if "status" in df.columns
        else {}
    )
    order_type_counts = (
        df.groupby("order_type").size().to_dict() if "order_type" in df.columns else {}
    )
    backlog_by_order_type = (
        backlog_df.groupby("order_type").size().to_dict()
        if "order_type" in backlog_df.columns
        else {}
    )

    return {
        "counts": {
            "total_orders": total_orders,
            "completed_orders": completed_orders,
            "backlog": backlog_count,
            "by_order_type": order_type_counts,
            "backlog_by_order_type": backlog_by_order_type,
            "status_counts": status_counts,
        },
        "kpis": {
            "completion_rate": completion_rate,
            "durations": durations,
            "durations_by_order_type": durations_by_order_type,
        },
        "aged_orders": aged_orders,
        "timeseries": timeseries,
    }

@app.get("/orders/aged")
def get_aged(aged_threshold_days: int = 3, limit: int = 100):
    df = load_orders()
    if df.empty:
        return {"aged_orders": []}
    backlog_df = df[df["completed_at"].isna()].copy()
    threshold_timestamp = pd.Timestamp.utcnow() - pd.Timedelta(days=aged_threshold_days)
    aged = backlog_df[
        backlog_df["created_at"].notna()
        & (backlog_df["created_at"] < threshold_timestamp)
    ]
    aged = aged.sort_values("created_at").head(limit)
    results = []
    for _, row in aged.iterrows():
        created_at = row["created_at"]
        age_days = (
            int((pd.Timestamp.utcnow() - created_at).total_seconds() // 86400)
            if pd.notna(created_at)
            else None
        )
        results.append(
            {
                "order_id": row["order_id"],
                "customer_name": row.get("customer_name"),
                "order_type": row.get("order_type"),
                "status": row.get("status"),
                "created_at": created_at.isoformat() if pd.notna(created_at) else None,
                "age_days": age_days,
            }
        )
    return {"aged_orders": results}