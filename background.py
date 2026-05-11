"""Background threads: salary scheduler, keep-alive ping."""
import threading


def _job_auto_generate_salary():
    """
    Check if today is the settlement day; if so, auto-generate last month's salary drafts.
    Uses pg_try_advisory_lock to ensure only one worker runs this in multi-worker deployments.
    """
    from datetime import date as _d, timedelta as _td
    import json as _jj
    import calendar as _calj

    today = _d.today()

    try:
        from blueprints.salary import _get_salary_config, _auto_generate_salary
        cfg = _get_salary_config()
    except Exception:
        cfg = {'settlement_day': 1, 'pay_day': 5}

    settlement_day = cfg['settlement_day']
    pay_day        = cfg['pay_day']

    days_in_cur_month   = _calj.monthrange(today.year, today.month)[1]
    effective_settlement = min(settlement_day, days_in_cur_month)
    if today.day != effective_settlement:
        return

    first  = today.replace(day=1)
    last_m = first - _td(days=1)
    month  = last_m.strftime('%Y-%m')

    effective_pay_day = min(pay_day, days_in_cur_month)
    pay_date_str = _d(today.year, today.month, effective_pay_day).isoformat()

    LOCK_KEY = 202604011

    try:
        from database import get_db
        from blueprints.salary import _auto_generate_salary
        with get_db() as conn:
            locked = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)
            ).fetchone()['ok']
            if not locked:
                return

            try:
                staff_list = conn.execute(
                    "SELECT * FROM punch_staff WHERE active=TRUE"
                ).fetchall()
                generated = 0
                for staff in staff_list:
                    data       = _auto_generate_salary(conn, dict(staff), month)
                    items_json = _jj.dumps(data['items'], ensure_ascii=False)
                    conn.execute("""
                        INSERT INTO salary_records
                          (staff_id, month, base_salary, insured_salary, work_days, actual_days,
                           leave_days, unpaid_days, ot_pay, allowance_total, deduction_total,
                           net_pay, items, pay_date, status, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'draft',NOW())
                        ON CONFLICT (staff_id, month) DO UPDATE
                          SET base_salary     = CASE WHEN salary_records.status='confirmed' THEN salary_records.base_salary     ELSE EXCLUDED.base_salary     END,
                              insured_salary  = CASE WHEN salary_records.status='confirmed' THEN salary_records.insured_salary  ELSE EXCLUDED.insured_salary  END,
                              work_days       = CASE WHEN salary_records.status='confirmed' THEN salary_records.work_days       ELSE EXCLUDED.work_days       END,
                              actual_days     = CASE WHEN salary_records.status='confirmed' THEN salary_records.actual_days     ELSE EXCLUDED.actual_days     END,
                              leave_days      = CASE WHEN salary_records.status='confirmed' THEN salary_records.leave_days      ELSE EXCLUDED.leave_days      END,
                              unpaid_days     = CASE WHEN salary_records.status='confirmed' THEN salary_records.unpaid_days     ELSE EXCLUDED.unpaid_days     END,
                              ot_pay          = CASE WHEN salary_records.status='confirmed' THEN salary_records.ot_pay          ELSE EXCLUDED.ot_pay          END,
                              allowance_total = CASE WHEN salary_records.status='confirmed' THEN salary_records.allowance_total ELSE EXCLUDED.allowance_total END,
                              deduction_total = CASE WHEN salary_records.status='confirmed' THEN salary_records.deduction_total ELSE EXCLUDED.deduction_total END,
                              net_pay         = CASE WHEN salary_records.status='confirmed' THEN salary_records.net_pay         ELSE EXCLUDED.net_pay         END,
                              items           = CASE WHEN salary_records.status='confirmed' THEN salary_records.items           ELSE EXCLUDED.items::jsonb    END,
                              pay_date        = COALESCE(salary_records.pay_date, EXCLUDED.pay_date),
                              status          = CASE WHEN salary_records.status='confirmed' THEN 'confirmed' ELSE 'draft' END,
                              updated_at      = NOW()
                    """, (
                        data['staff_id'], month, data['base_salary'], data['insured_salary'],
                        data['work_days'], data['actual_days'], data['leave_days'], data['unpaid_days'],
                        data['ot_pay'], data['allowance_total'], data['deduction_total'],
                        data['net_pay'], items_json, pay_date_str,
                    ))
                    generated += 1
                print(f'[scheduler] 薪資自動產生完成：{month}，發薪日 {pay_date_str}，共 {generated} 筆', flush=True)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    except Exception as e:
        print(f'[scheduler] 薪資自動產生失敗：{e}', flush=True)


def start_salary_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = BackgroundScheduler(timezone='Asia/Taipei')
        scheduler.add_job(
            _job_auto_generate_salary,
            trigger=CronTrigger(hour=2, minute=0, timezone='Asia/Taipei'),
            id='monthly_salary_generate',
            replace_existing=True,
        )
        scheduler.start()
        print('[scheduler] 薪資自動產生排程已啟動（每日 02:00 TW 檢查結算日）', flush=True)
    except Exception as e:
        print(f'[scheduler] 排程啟動失敗：{e}', flush=True)


def start_keep_alive():
    """Ping self every 25 minutes to keep Render free tier awake."""
    import os, time, urllib.request
    app_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not app_url:
        return

    def _ping():
        while True:
            time.sleep(25 * 60)
            try:
                urllib.request.urlopen(f'{app_url}/health', timeout=10)
            except Exception:
                pass

    t = threading.Thread(target=_ping, daemon=True)
    t.start()
