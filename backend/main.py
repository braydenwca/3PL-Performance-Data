from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import sqlite3
from io import BytesIO
from datetime import datetime, timedelta
import os
from pydantic import BaseModel
from typing import Optional, List

DB_PATH = "data/orders.db"
TABLE_NAME = "orders"

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
    # We'll store raw timestamps as ISO strings and keep columns flexible.
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        order_id TEXT PRIMARY KEY,
        created_at TEXT,
        picked_at TEXT,
        packed_at TEXT,
        shipped_at TEXT,
        completed_at TEXT,
        status TEXT,
        raw_json TEXT
    )"""
    )
    conn.commit()
    conn.close()

ensure_db()

@app.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    """
    Accepts a CSV with order rows. Columns expected: order_id, created_at, picked_at, packed_at, shipped_at, completed_at, status (optional)
    Existing order_id rows will be upserted.
    """
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    contents = await file.read()
    try:
        df = pd.read_csv(BytesIO(contents), dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {e}")

    required = {"order_id", "created_at"}
    if not required.issubset(set(df.columns)):
        raise HTTPException(status_code=400, detail=f"CSV missing required columns: {required}")

    # Normalize columns to ensure presence
    for c in ["picked_at", "packed_at", "shipped_at", "completed_at", "status"]:
        if c not in df.columns:
            df[c] = None

    # Keep raw JSON per row for flexibility
    df["raw_json"] = df.apply(lambda r: r.to_json(), axis=1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    upsert_sql = f"""
    INSERT INTO {TABLE_NAME} (order_id, created_at, picked_at, packed_at, shipped_at, completed_at, status, raw_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(order_id) DO UPDATE SET
      created_at=excluded.created_at,
      picked_at=excluded.picked_at,
      packed_at=excluded.packed_at,
      shipped_at=excluded.shipped_at,
      completed_at=excluded.completed_at,
      status=excluded.status,
      raw_json=excluded.raw_json
    """
    rows = []
    for _, r in df.iterrows():
        rows.append((
            str(r["order_id"]),
            r["created_at"] if pd.notna(r["created_at"]) else None,
            r["picked_at"] if pd.notna(r["picked_at"]) else None,
            r["packed_at"] if pd.notna(r["packed_at"]) else None,
            r["shipped_at"] if pd.notna(r["shipped_at"]) else None,
            r["completed_at"] if pd.notna(r["completed_at"]) else None,
            r["status"] if pd.notna(r["status"]) else None,
            r["raw_json",
        ))
    cur.executemany(upsert_sql, rows)
    conn.commit()
    conn.close()
    return {"imported": len(rows)}

def load_orders(start: Optional[str]=None, end: Optional[str]=None):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(f"SELECT * FROM {TABLE_NAME}", conn)
    conn.close()
    if df.empty:
        return df
    # parse timestamps to datetimes
    for col in ["created_at","picked_at","packed_at","shipped_at","completed_at"]:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    if start:
        s = pd.to_datetime(start, utc=True)
        df = df[df["created_at"] >= s]
    if end:
        e = pd.to_datetime(end, utc=True)
        df = df[df["created_at"] <= e]
    return df

class MetricsParams(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    aged_threshold_days: Optional[int] = Query(3)

@app.get("/metrics")
def metrics(start: Optional[str] = Query(None), end: Optional[str] = Query(None), aged_threshold_days: int = Query(3)):
    """
    Compute dashboard metrics. Query params:
    - start, end: ISO dates to filter by created_at
    - aged_threshold_days: highlight orders older than this (not completed)
    """
    df = load_orders(start, end)
    if df.empty:
        return {"counts":{}, "kpis":{}, "aged_orders":[], "timeseries":{}}

    # Completion and backlog
    total = len(df)
    completed = df["completed_at"].notna().sum()
    completion_rate = completed / total

    # Stage durations (in hours)
    def delta_hours(a, b):
        return (df[b] - df[a]).dt.total_seconds() / 3600

    durations = {}
    stage_pairs = [
        ("created_at","picked_at", "created_to_picked"),
        ("picked_at","packed_at", "picked_to_packed"),
        ("packed_at","shipped_at", "packed_to_shipped"),
        ("shipped_at","completed_at", "shipped_to_completed"),
        ("created_at","completed_at", "created_to_completed"),
    ]
    for a,b,name in stage_pairs:
        if a in df.columns and b in df.columns:
            series = delta_hours(a,b)
            # only keep positive non-null durations
            series = series[series.notna() & (series >= 0)]
            durations[name] = {
                "count": int(series.count()),
                "mean_hours": float(series.mean()) if not series.empty else None,
                "median_hours": float(series.median()) if not series.empty else None,
                "p90_hours": float(series.quantile(0.9)) if not series.empty else None
            }

    # Backlog: orders not completed
    backlog_df = df[df["completed_at"].isna()]
    backlog_count = len(backlog_df)

    # Aged orders: created_at older than threshold days and not completed
    threshold_date = pd.Timestamp.utcnow() - pd.Timedelta(days=aged_threshold_days)
    aged = backlog_df[backlog_df["created_at"] < threshold_date]
    aged_list = []
    for _, r in aged.sort_values("created_at").iterrows():
        aged_list.append({
            "order_id": r["order_id"],
            "created_at": r["created_at"].isoformat() if pd.notna(r["created_at"]) else None,
            "status": r["status"],
            "age_days": (pd.Timestamp.utcnow() - r["created_at"]).days if pd.notna(r["created_at"]) else None
        })

    # Throughput: completed per day
    if df["completed_at"].notna().any():
        completed_df = df[df["completed_at"].notna()].copy()
        completed_df['date'] = completed_df['completed_at'].dt.date
        throughput = completed_df.groupby('date').size().reset_index(name='completed')
        timeseries = {
            "throughput": throughput.to_dict(orient="records")
        }
    else:
        timeseries = {"throughput": []}

    # Queue by status
    status_counts = df.groupby(df["status"].fillna("unknown")).size().to_dict()

    # SLA breaches: example: created_to_picked > 24h
    sla_breaches = {}
    if "created_to_picked" in durations and durations["created_to_picked"]["count"] > 0:
        # find orders where created->picked > 24 hours
        cond = delta_hours("created_at","picked_at") > 24
        sla_breaches["created_to_picked_gt_24h"] = int(cond.sum())

    return {
        "counts": {
            "total_orders": int(total),
            "completed_orders": int(completed),
            "backlog": int(backlog_count),
        },
        "kpis": {
            "completion_rate": completion_rate,
            "durations": durations,
            "status_counts": status_counts,
            "sla_breaches": sla_breaches,
        },
        "aged_orders": aged_list,
        "timeseries": timeseries,
    }

@app.get("/orders/aged")
def get_aged(aged_threshold_days: int = 3, limit: int = 100):
    df = load_orders()
    backlog_df = df[df["completed_at"].isna()]
    threshold_date = pd.Timestamp.utcnow() - pd.Timedelta(days=aged_threshold_days)
    aged = backlog_df[backlog_df["created_at"] < threshold_date]
    aged = aged.sort_values("created_at").head(limit)
    result = []
    for _, r in aged.iterrows():
        result.append({
            "order_id": r["order_id"],
            "created_at": r["created_at"].isoformat() if pd.notna(r["created_at"]) else None,
            "status": r["status"],
            "age_days": int((pd.Timestamp.utcnow() - r["created_at"]).days) if pd.notna(r["created_at"]) else None
        })
    return {"aged_orders": result}