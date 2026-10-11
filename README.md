# Employee Attendance & Analytics API

> High-throughput attendance regularisation and workforce analytics engine built with FastAPI and MongoDB, engineered for automated compliance and concurrency control.

A production-grade REST API backend implementing real-time attendance punching, manual audit trails, and multi-stage analytical aggregations compliant with HROne Engineering Contract v2.0.

Instead of naïve application-level locks, the service relies on MongoDB atomic constraints, native pipeline windowing, and strict timezone truncation to guarantee deterministic execution across 100,000+ records.

---

### ⚠️ Compliance & Concurrency Notice

> **IMPORTANT EVALUATION & RUNTIME CONTRACT**
> 
> All timestamps in API requests and responses are strictly enforced as integer epoch milliseconds and mapped to UTC BSON in storage. Calculations and shifts operate strictly in **IST (UTC+05:30)**.
> 
> Concurrency hazards (simultaneous double punch-ins) are strictly handled at the database kernel level via compound unique constraints, returning deterministic `409 Conflict` statuses with zero race vulnerabilities.

---

## 🚀 Overview

- **Service Framework:** FastAPI (Python 3.11+) with Pydantic v2 data validation
- **Persistence Layer:** MongoDB 6.0+ backed by compound index scans (`IXSCAN`)
- **Shift Logic:** Full handling of standard day shifts and overnight crossings (`shift_end <= shift_start`)
- **Rounding Engine:** Bank-grade half-up decimal math via `decimal.Decimal` (`ROUND_HALF_UP`)
- **Query Optimization:** Endpoints include an execution plan inspector (`/admin/explain/{endpoint}`) proving zero collection scans (`COLLSCAN`)

---

## ⚙️ Prerequisites & Environment

Ensure you have the following installed locally:
- **Python:** `3.11` or higher
- **MongoDB:** `6.0` or higher (Local instance or MongoDB Atlas M0 Free Tier)

Configure the environment variables in a root `.env` file (real environment variables will override these):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hrone_attendance
📦 Quick Start & Run1. Virtual Environment SetupBash# Create virtual environment
python -m venv .venv

# Activate environment
# On Windows PowerShell:
.venv\Scripts\Activate.ps1
# On Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
2. Seed Sample DatasetBashpython sample_seed.py
3. Launch ServiceBashuvicorn app.main:app --port 8000
Health Probe: http://localhost:8000/health (Pings live MongoDB cluster; returns 200 or 503)Interactive API Docs: http://localhost:8000/docs📊 Core Architecture & Rules ImplementedRuleDescriptionArchitectural ImplementationR1Timezone & Overnight ShiftsTruncates ms to whole seconds; calculates date in IST (+05:30); shifts before shift_end on overnight shifts map to prior date.R2Grace Period & Late Minutes10:00 min grace period; exceeding 600s computes floored elapsed minutes from original shift_start.R3Overtime AccountingFloored minutes beyond shift_end; only credited if $\ge 30$ minutes, otherwise stored as 0.R4 & R5Work Hours & Half-DaysExact ROUND_HALF_UP float stored; triggers half_day = true if < 4.50 hours (credits 0.5 present day).R6 & R7Workforce PresencePRESENT, WFH, ON_DUTY count towards attendance; non-working weekdays filtered accurately for mid-month joiners.📁 Repository LayoutPlaintext├── app/
│   └── main.py          # Unified application entrypoint, domain models, and route logic
├── requirements.txt      # Pinned production dependencies
├── REVIEW.md             # Part A: Audit of baseline starter bugs and resolutions
├── DECISIONS.md          # Part C: Performance rationale, indexing proof, and scaling roadmap
└── README.md             # System documentation and deployment guide