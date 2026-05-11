"""
Flask application factory — thin entry point.
All routes live in blueprints/*.py; this file only wires them together.
"""
import os

from flask import Flask, jsonify

from config import SECRET_KEY, DATABASE_URL
from database import init_pool, init_all_db


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = SECRET_KEY
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB upload limit

    # ── Blueprint registration ─────────────────────────────────────
    from blueprints.admin       import bp as admin_bp
    from blueprints.punch       import bp as punch_bp
    from blueprints.line_bot    import bp as line_bp
    from blueprints.schedule    import bp as schedule_bp
    from blueprints.overtime    import bp as overtime_bp
    from blueprints.leave       import bp as leave_bp
    from blueprints.salary      import bp as salary_bp
    from blueprints.announcement import bp as ann_bp
    from blueprints.holiday     import bp as holiday_bp
    from blueprints.export      import bp as export_bp
    from blueprints.stores      import bp as stores_bp
    from blueprints.finance     import bp as finance_bp
    from blueprints.expense     import bp as expense_bp
    from blueprints.performance import bp as perf_bp
    from blueprints.training    import bp as training_bp
    from blueprints.mobile      import bp as mobile_bp
    from blueprints.webauthn    import bp as webauthn_bp

    for bp in (admin_bp, punch_bp, line_bp, schedule_bp, overtime_bp,
               leave_bp, salary_bp, ann_bp, holiday_bp, export_bp,
               stores_bp, finance_bp, expense_bp, perf_bp, training_bp,
               mobile_bp, webauthn_bp):
        app.register_blueprint(bp)

    # ── Health check ───────────────────────────────────────────────
    @app.route('/health')
    def health():
        try:
            from database import get_db
            with get_db() as conn:
                conn.execute('SELECT 1')
            return jsonify({'status': 'ok', 'db': 'connected'}), 200
        except Exception as e:
            return jsonify({'status': 'error', 'detail': str(e)}), 500

    return app


# ── Application startup ────────────────────────────────────────────────────────

# Initialise connection pool and DB schema (idempotent, safe to call every time)
if DATABASE_URL:
    try:
        init_pool()
    except Exception as _pe:
        print(f'[startup] pool init failed: {_pe}')

try:
    init_all_db()
except Exception as _de:
    print(f'[startup] DB init failed: {_de}')

# Build the WSGI app object that gunicorn / the dev server will use
app = create_app()

# ── Background tasks ───────────────────────────────────────────────────────────

try:
    from background import start_salary_scheduler
    start_salary_scheduler()
except Exception as _se:
    print(f'[startup] scheduler failed: {_se}')

try:
    from background import start_keep_alive
    start_keep_alive()
except Exception:
    pass

# ── Annual leave sync ──────────────────────────────────────────────────────────
# Sync annual leave quotas once at startup (runs in a background thread).

def _sync_annual_leave_once():
    import threading, time
    def _run():
        time.sleep(5)  # wait for DB to settle
        try:
            from helpers import _calc_annual_leave_days
            from database import get_db
            from datetime import date
            year = date.today().year
            with get_db() as conn:
                staff_list = conn.execute(
                    "SELECT id, hire_date FROM punch_staff WHERE active=TRUE AND hire_date IS NOT NULL"
                ).fetchall()
                leave_type = conn.execute(
                    "SELECT id FROM leave_types WHERE code='annual' LIMIT 1"
                ).fetchone()
                if not leave_type:
                    return
                ltid = leave_type['id']
                for s in staff_list:
                    days = _calc_annual_leave_days(str(s['hire_date']), year)
                    if days is None:
                        continue
                    conn.execute("""
                        INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (staff_id, leave_type_id, year)
                        DO UPDATE SET total_days=EXCLUDED.total_days
                    """, (s['id'], ltid, year, days))
        except Exception as e:
            print(f'[startup] annual leave sync: {e}')
    threading.Thread(target=_run, daemon=True).start()


_sync_annual_leave_once()

# ── Dev server entry point ─────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
