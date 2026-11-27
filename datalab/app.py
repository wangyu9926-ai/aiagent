"""Read-only data analysis worker.

The portal sends file paths under /uploads and a SELECT/WITH query. Files are
loaded with pandas and registered as DuckDB tables; the worker never executes
arbitrary Python and never writes to the upload tree.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import hmac
import io
import math
import os
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import duckdb
import matplotlib

matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

UPLOAD_ROOT = Path(os.environ.get("DATALAB_UPLOAD_ROOT", "/uploads")).resolve()
TOKEN = os.environ.get("DATALAB_TOKEN", "").strip()
MAX_FILE_BYTES = int(os.environ.get("DATALAB_MAX_FILE_BYTES", str(100 * 1024 * 1024)))
MAX_TOTAL_BYTES = int(os.environ.get("DATALAB_MAX_TOTAL_BYTES", str(200 * 1024 * 1024)))
MAX_TABLES = int(os.environ.get("DATALAB_MAX_TABLES", "10"))
DEFAULT_ROWS = int(os.environ.get("DATALAB_DEFAULT_ROWS", "200"))
MAX_ROWS = int(os.environ.get("DATALAB_MAX_ROWS", "2000"))
TIMEOUT_SECONDS = float(os.environ.get("DATALAB_TIMEOUT", "60"))

BLOCKED_SQL = re.compile(
    r"(?is)\b(attach|detach|copy|export|import|install|load|pragma|call|execute|"
    r"create|insert|update|delete|drop|alter|truncate|merge|"
    r"read_csv|read_csv_auto|read_parquet|read_json|read_json_auto|"
    r"glob|httpfs|sqlite_scan|postgres_scan|mysql_scan|getenv)\b"
)

app = FastAPI(title="Hermes DataLab", docs_url=None, redoc_url=None, openapi_url=None)


class TableSpec(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    table: str = Field(default="", max_length=80)
    sheet: str | int | None = None


class ChartSpec(BaseModel):
    type: Literal["bar", "line", "pie", "scatter"] = "bar"
    x: str = Field(min_length=1, max_length=120)
    y: list[str] | str | None = None
    title: str = Field(default="", max_length=200)


class QueryIn(BaseModel):
    tables: list[TableSpec] = Field(min_length=1, max_length=MAX_TABLES)
    sql: str = Field(min_length=1, max_length=50_000)
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)
    chart: ChartSpec | None = None


def _authorized(request: Request) -> bool:
    if not TOKEN:
        return False
    supplied = request.headers.get("x-datalab-token", "").strip()
    if not supplied:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, TOKEN)


def _safe_path(relative: str) -> Path:
    pure = PurePosixPath(str(relative).replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError("path is invalid")
    full = (UPLOAD_ROOT / Path(*pure.parts)).resolve()
    if not full.is_relative_to(UPLOAD_ROOT) or not full.is_file():
        raise ValueError("file does not exist")
    return full


def _table_name(raw: str, index: int) -> str:
    name = (raw or "").strip() or f"t{index + 1}"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid table name: {name}")
    return name


def _clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    seen: dict[str, int] = {}
    columns: list[str] = []
    for index, raw in enumerate(frame.columns):
        base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(raw).strip()).strip("_")
        base = base or f"column_{index + 1}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        columns.append(base if count == 1 else f"{base}_{count}")
    frame.columns = columns
    return frame


def _read_table(path: Path, sheet: str | int | None) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path, ",")
    if suffix == ".tsv":
        return _read_csv(path, "\t")
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
    if suffix == ".json":
        return pd.read_json(path)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"unsupported file type: {suffix or '(none)'}")


def _read_csv(path: Path, separator: str) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030", "latin1"):
        try:
            return pd.read_csv(path, sep=separator, encoding=encoding, low_memory=False)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"could not decode CSV: {last_error}")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except Exception:
            pass
    return str(value)


def _check_sql(sql: str) -> str:
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not re.match(r"(?is)^(select|with)\b", text):
        raise ValueError("only SELECT/WITH queries are allowed")
    if ";" in text:
        raise ValueError("only one statement is allowed")
    if BLOCKED_SQL.search(text):
        raise ValueError("query uses a blocked keyword or file function")
    return text


def _render_chart(frame: pd.DataFrame, spec: ChartSpec) -> tuple[str, str]:
    """Return (base64 png, mime) for a small result set."""
    if frame.empty:
        raise ValueError("query returned no rows to chart")
    if spec.x not in frame.columns:
        raise ValueError(f"chart x column not found: {spec.x}")
    if spec.y is None:
        ys = [str(column) for column in frame.columns if column != spec.x][:4]
    elif isinstance(spec.y, str):
        ys = [spec.y]
    else:
        ys = [str(item) for item in spec.y]
    ys = [column for column in ys if column]
    if not ys:
        raise ValueError("chart needs at least one y column")
    for column in ys:
        if column not in frame.columns:
            raise ValueError(f"chart y column not found: {column}")

    plot = frame.head(1000).copy()
    x_values = plot[spec.x].astype(str).tolist()
    figure = Figure(figsize=(8.2, 4.6), dpi=140)
    canvas = FigureCanvasAgg(figure)
    axes = figure.subplots()
    if spec.type == "pie":
        values = pd.to_numeric(plot[ys[0]], errors="coerce").fillna(0)
        axes.pie(values.tolist(), labels=x_values, autopct="%1.1f%%", textprops={"fontsize": 8})
        axes.axis("equal")
    else:
        for column in ys:
            values = pd.to_numeric(plot[column], errors="coerce")
            if spec.type == "line":
                axes.plot(x_values, values, marker="o", linewidth=1.8, label=column)
            elif spec.type == "scatter":
                axes.scatter(x_values, values, label=column, alpha=0.8)
            else:
                axes.bar(x_values, values, label=column, alpha=0.85)
        if len(ys) > 1:
            axes.legend(fontsize=8)
        axes.grid(True, axis="y", alpha=0.2)
        axes.tick_params(axis="x", labelrotation=35, labelsize=8)
        axes.tick_params(axis="y", labelsize=8)
    if spec.title:
        axes.set_title(spec.title, fontsize=11)
    figure.tight_layout()
    canvas.draw()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    payload = buffer.getvalue()
    if len(payload) > 5 * 1024 * 1024:
        raise ValueError("chart image is too large")
    return base64.b64encode(payload).decode("ascii"), "image/png"


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "duckdb": duckdb.__version__, "pandas": pd.__version__}


@app.post("/query")
def query(body: QueryIn, request: Request):
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    started = time.monotonic()
    try:
        sql = _check_sql(body.sql)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    connection = duckdb.connect()
    timer: threading.Timer | None = None
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='1GB'")
        total_bytes = 0
        table_info: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for index, spec in enumerate(body.tables):
            path = _safe_path(spec.path)
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f"{path.name} exceeds the file size limit")
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("total input size exceeds the limit")
            table = _table_name(spec.table, index)
            if table in seen_names:
                raise ValueError(f"duplicate table name: {table}")
            seen_names.add(table)
            frame = _clean_columns(_read_table(path, spec.sheet))
            connection.register(table, frame)
            table_info.append(
                {"table": table, "rows": int(len(frame)), "columns": [str(c) for c in frame.columns]}
            )

        timer = threading.Timer(TIMEOUT_SECONDS, connection.interrupt)
        timer.daemon = True
        timer.start()
        cursor = connection.execute(sql)
        columns = [str(item[0]) for item in (cursor.description or [])]
        rows = cursor.fetchmany(body.limit + 1)
        truncated = len(rows) > body.limit
        rows = rows[: body.limit]
        response: dict[str, Any] = {
            "columns": columns,
            "rows": [[_json_value(value) for value in row] for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "tables": table_info,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
        if body.chart is not None:
            try:
                png, mime = _render_chart(pd.DataFrame(rows, columns=columns), body.chart)
                response["chart_png_base64"] = png
                response["chart_mime"] = mime
            except Exception as exc:
                response["chart_error"] = str(exc)[:300]
        return response
    except Exception as exc:
        message = str(exc).strip().replace("\n", " ")[:600]
        return JSONResponse(status_code=400, content={"error": message or type(exc).__name__})
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()
