# 3PL Performance Dashboard — Prototype

Overview
- This prototype provides a simple upload + dashboard interface to ingest order data (CSV) and visualize performance metrics such as time-in-stage, completion rates, queue backlog, aged orders, throughput, and simple trends.
- Backend: FastAPI (Python) — accepts CSV upload, stores orders in SQLite, computes metrics via pandas.
- Frontend: React — file upload UI, date range picker, dashboard panels + charts.
- Goal: Minimal, extendable foundation you can customize and harden for production.

Key features
- Upload CSV of orders with timestamps for stage events (created, picked, packed, shipped, completed).
- Compute metrics:
  - Average/median stage durations (created→picked, picked→packed, packed→shipped, shipped→completed)
  - Completion rate (orders completed / total orders)
  - Queue backlog (open orders by age and status)
  - Aged orders list (orders older than a configurable threshold)
  - Throughput (orders completed per day)
  - SLA breach counts (orders exceeding thresholds)
- Dashboard shows numeric KPIs and simple charts.

CSV format (required columns)
- order_id (string or number)
- created_at (ISO 8601 timestamp)
- picked_at (ISO 8601 timestamp or blank)
- packed_at (ISO 8601 timestamp or blank)
- shipped_at (ISO 8601 timestamp or blank)
- completed_at (ISO 8601 timestamp or blank)
- status (e.g., created/picked/packed/shipped/completed/cancelled) — optional, can be derived

Example row
order_id,created_at,picked_at,packed_at,shipped_at,completed_at,status
12345,2025-11-01T09:12:03Z,2025-11-01T10:00:00Z,2025-11-01T11:30:00Z,2025-11-01T12:45:00Z,2025-11-01T13:00:00Z,completed

How to run (local prototype)
1. Backend
   - cd backend
   - python -m venv .venv && source .venv/bin/activate
   - pip install -r requirements.txt
   - uvicorn main:app --reload --port 8000
2. Frontend
   - cd frontend
   - npm install
   - npm start (runs at http://localhost:3000)
3. Upload a CSV via the frontend. The backend persists orders to `data/orders.db` (SQLite).

Notes & next steps
- Authentication, rate-limiting, and validation should be added for production.
- Consider adding user roles, multi-warehouse support, and async background processing for large files.
- Add tests and CI/CD and containerization (Docker) if you want a production-ready artifact.