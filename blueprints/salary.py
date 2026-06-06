import ast as _ast
import json as _json
import operator as _op
from functools import wraps

from flask import Blueprint, request, jsonify, session

from auth import require_module
from database import get_db
from helpers import _notify_review_result, _calc_annual_leave_days

bp = Blueprint('salary', __name__)


# ── DB Init ───────────────────────────────────────────────────────────────────

def init_salary_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS salary_items (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            item_type   TEXT NOT NULL DEFAULT 'allowance',
            formula     TEXT DEFAULT '',
            amount      NUMERIC(12,2) DEFAULT 0,
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#4a7bda',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_ids JSONB DEFAULT NULL",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_overrides JSONB DEFAULT NULL",
        """CREATE TABLE IF NOT EXISTS salary_records (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            month           TEXT NOT NULL,
            base_salary     NUMERIC(12,2) DEFAULT 0,
            insured_salary  NUMERIC(12,2) DEFAULT 0,
            work_days       NUMERIC(5,1)  DEFAULT 0,
            actual_days     NUMERIC(5,1)  DEFAULT 0,
            leave_days      NUMERIC(5,1)  DEFAULT 0,
            unpaid_days     NUMERIC(5,1)  DEFAULT 0,
            ot_pay          NUMERIC(12,2) DEFAULT 0,
            allowance_total NUMERIC(12,2) DEFAULT 0,
            deduction_total NUMERIC(12,2) DEFAULT 0,
            net_pay         NUMERIC(12,2) DEFAULT 0,
            items           JSONB         DEFAULT '[]',
            status          TEXT          DEFAULT 'draft',
            note            TEXT          DEFAULT '',
            confirmed_by    TEXT          DEFAULT '',
            confirmed_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ   DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   DEFAULT NOW(),
            UNIQUE(staff_id, month)
        )""",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS income_tax_withheld NUMERIC(12,2) DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS salary_config (
            id              INT PRIMARY KEY DEFAULT 1,
            settlement_day  INT DEFAULT 1,
            pay_day         INT DEFAULT 5,
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        "INSERT INTO salary_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS pay_date DATE",
        "UPDATE salary_items SET active=FALSE WHERE name='勞退提撥6%' AND item_type='deduction'",
        "UPDATE salary_items SET active=FALSE WHERE name='勞退6%' AND item_type='allowance'",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[salary_init] {str(e)[:80]}")

    defaults = [
        ('本薪',        'allowance', 'base_salary+service_years*1000', 0,    '#2e9e6b', 1,  True),
        ('職務加給',    'allowance', '',                                0,    '#0ea5e9', 2,  True),
        ('全勤獎金',    'allowance', '',                                0,    '#c8a96e', 3,  True),
        ('獎金',        'allowance', '',                                0,    '#8b5cf6', 4,  True),
        ('生日禮金',    'allowance', '',                                1000, '#e05c8a', 5,  True),
        ('勞退6%',      'allowance', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 6, False),
        ('病/事/假',    'deduction', '',                                0,    '#8892a4', 7,  True),
        ('勞保費',      'deduction', 'insured_salary*0.125*0.2',       0,    '#d64242', 8,  True),
        ('健保費',      'deduction', 'insured_salary*0.0517*0.3',      0,    '#e07b2a', 9,  True),
        ('勞退提撥6%',  'deduction', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 10, False),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM salary_items").fetchone()['c']
            if cnt == 0:
                for name, itype, formula, amount, color, sort, active in defaults:
                    conn.execute("""
                        INSERT INTO salary_items (name,item_type,formula,amount,color,sort_order,active)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """, (name, itype, formula, amount, color, sort, active))
    except Exception as e:
        print(f"[salary_seed] {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_salary_config(conn=None):
    def _query(c):
        row = c.execute("SELECT * FROM salary_config WHERE id=1").fetchone()
        if not row:
            return {'settlement_day': 1, 'pay_day': 5}
        return {
            'settlement_day': int(row['settlement_day'] or 1),
            'pay_day':        int(row['pay_day']        or 5),
        }
    if conn:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def salary_item_row(row):
    if not row: return None
    d = dict(row)
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def salary_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['base_salary', 'insured_salary', 'work_days', 'actual_days', 'leave_days',
              'unpaid_days', 'ot_pay', 'allowance_total', 'deduction_total', 'net_pay']:
        if d.get(f) is not None: d[f] = float(d[f])
    if isinstance(d.get('items'), str):
        try: d['items'] = _json.loads(d['items'])
        except: d['items'] = []
    w = float(d.get('work_days') or 0)
    l = float(d.get('leave_days') or 0)
    a = float(d.get('actual_days') or 0)
    d['absent_days'] = max(0.0, round(w - l - a, 1))
    items = d.get('items') or []
    hourly_item = next((i for i in items if i.get('id') == 'hourly_base'), None)
    d['hourly_base_pay'] = float(hourly_item['amount']) if hourly_item else 0.0
    if d.get('pay_date'):     d['pay_date']     = d['pay_date'].isoformat()
    if d.get('confirmed_at'): d['confirmed_at'] = d['confirmed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    if d.get('updated_at'):   d['updated_at']   = d['updated_at'].isoformat()
    return d


def _eval_formula(formula, base_salary, insured_salary, service_years):
    if not formula: return 0.0
    _vars = {
        'base_salary':    float(base_salary or 0),
        'insured_salary': float(insured_salary or 0),
        'service_years':  float(service_years or 0),
    }
    _ops = {
        _ast.Add:  _op.add,  _ast.Sub: _op.sub,
        _ast.Mult: _op.mul,  _ast.Div: _op.truediv,
        _ast.Pow:  _op.pow,  _ast.Mod: _op.mod,
        _ast.USub: _op.neg,  _ast.UAdd: _op.pos,
    }
    def _safe_eval(node):
        if isinstance(node, _ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError('非數字常數')
            return float(node.value)
        if isinstance(node, _ast.Name):
            if node.id not in _vars:
                raise ValueError(f'未知變數: {node.id}')
            return _vars[node.id]
        if isinstance(node, _ast.BinOp):
            fn = _ops.get(type(node.op))
            if not fn: raise ValueError('不支援的運算子')
            return fn(_safe_eval(node.left), _safe_eval(node.right))
        if isinstance(node, _ast.UnaryOp):
            fn = _ops.get(type(node.op))
            if not fn: raise ValueError('不支援的一元運算子')
            return fn(_safe_eval(node.operand))
        raise ValueError(f'不支援的語法: {type(node).__name__}')
    try:
        tree = _ast.parse(formula.strip(), mode='eval')
        return round(float(_safe_eval(tree.body)), 2)
    except Exception:
        return 0.0


def _calc_service_years(hire_date_str):
    if not hire_date_str: return 0.0
    from datetime import date as _d
    try:
        hire = _d.fromisoformat(str(hire_date_str))
        return round((_d.today() - hire).days / 365.25, 2)
    except Exception:
        return 0.0


def _auto_generate_salary(conn, staff, month, work_days=None):
    import calendar as _cal
    from datetime import date as _d, timedelta as _td, datetime as _dts, timezone as _tz
    from blueprints.punch import _build_shift_time_map, _clamp_to_shift, _shift_aware_day_map, _calc_punch_hours
    _TW = _tz(_td(hours=8))
    _today = _dts.now(_TW).date()
    y, m = int(month[:4]), int(month[5:])
    total_work_days = work_days
    scheduled_dates = set()

    if total_work_days is None:
        shift_date_rows = conn.execute("""
            SELECT DISTINCT shift_date FROM shift_assignments
            WHERE staff_id=%s AND TO_CHAR(shift_date,'YYYY-MM')=%s
            ORDER BY shift_date
        """, (staff['id'], month)).fetchall()
        if shift_date_rows:
            scheduled_dates = {
                r['shift_date'].isoformat() if hasattr(r['shift_date'], 'isoformat') else str(r['shift_date'])
                for r in shift_date_rows
            }
            total_work_days = len(scheduled_dates)
        else:
            holiday_rows = conn.execute("""
                SELECT date FROM public_holidays
                WHERE TO_CHAR(date,'YYYY-MM')=%s
            """, (month,)).fetchall()
            holiday_dates = {
                r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date'])
                for r in holiday_rows
            }
            days_in_month = _cal.monthrange(y, m)[1]
            for _day in range(1, days_in_month + 1):
                _dt = _d(y, m, _day)
                _ds = _dt.isoformat()
                if _dt.weekday() not in (5, 6) and _ds not in holiday_dates:
                    scheduled_dates.add(_ds)
            total_work_days = len(scheduled_dates)

    salary_type    = staff.get('salary_type', 'monthly') or 'monthly'
    base_salary    = float(staff.get('base_salary')    or 0)
    hourly_rate    = float(staff.get('hourly_rate')    or 0)
    insured_salary = float(staff.get('insured_salary') or base_salary)
    daily_hours    = float(staff.get('daily_hours')    or 8)
    service_years  = _calc_service_years(staff.get('hire_date'))

    actual_work_hours = 0.0
    punch_details     = []
    if salary_type == 'hourly':
        actual_work_hours, punch_work_days, punch_details = _calc_punch_hours(
            conn, staff['id'], month
        )
        hourly_base_pay = round(actual_work_hours * hourly_rate, 2)
    else:
        hourly_base_pay = 0.0

    ot_rows = conn.execute("""
        SELECT COALESCE(SUM(ot_pay), 0) as total
        FROM overtime_requests
        WHERE staff_id=%s AND status='approved'
          AND to_char(request_date,'YYYY-MM')=%s
    """, (staff['id'], month)).fetchone()
    ot_pay = float(ot_rows['total']) if ot_rows else 0.0

    month_first = _d(y, m, 1)
    month_last  = _d(y, m, _cal.monthrange(y, m)[1])

    leave_rows = conn.execute("""
        SELECT lr.total_days, lt.pay_rate, lt.code, lt.name as leave_name,
               lr.start_date, lr.end_date
        FROM leave_requests lr
        JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.staff_id=%s AND lr.status='approved'
          AND lr.start_date <= %s AND lr.end_date >= %s
    """, (staff['id'], month_last, month_first)).fetchall()

    def _leave_days_in_month(row):
        sd = row['start_date']
        ed = row['end_date']
        if hasattr(sd, 'date'): sd = sd.date()
        else: sd = _d.fromisoformat(str(sd))
        if hasattr(ed, 'date'): ed = ed.date()
        else: ed = _d.fromisoformat(str(ed))
        if sd >= month_first and ed <= month_last:
            return float(row['total_days'])
        cur = max(sd, month_first)
        end = min(ed, month_last)
        cnt = 0.0
        while cur <= end:
            if cur.weekday() != 6:
                cnt += 1.0
            cur += _td(days=1)
        return cnt

    leave_days  = sum(_leave_days_in_month(r) for r in leave_rows)
    unpaid_days = sum(_leave_days_in_month(r) for r in leave_rows if float(r['pay_rate']) == 0)
    half_pay_rows = [
        (_leave_days_in_month(r), float(r['pay_rate']), r['leave_name'])
        for r in leave_rows if 0 < float(r['pay_rate']) < 1
    ]
    actual_days = total_work_days - leave_days

    if salary_type == 'hourly':
        daily_wage  = hourly_rate * daily_hours
        hourly_wage = hourly_rate
    else:
        daily_wage  = base_salary / 30 if base_salary > 0 else 0
        hourly_wage = daily_wage / daily_hours if daily_hours > 0 else 0

    items           = []
    allowance_total = 0.0
    deduction_total = 0.0
    _overrides = staff.get('salary_item_overrides') or {}
    if isinstance(_overrides, str):
        try: _overrides = _json.loads(_overrides)
        except Exception: _overrides = {}

    def _apply_override(item_id, calculated_amt):
        key = str(item_id)
        if key in _overrides and _overrides[key] is not None and _overrides[key] != '':
            return float(_overrides[key]), True
        return calculated_amt, False

    if salary_type == 'hourly':
        items.append({
            'id': 'hourly_base', 'name': '本薪（工時）', 'type': 'allowance',
            'amount': hourly_base_pay, 'formula': '',
            'calc_note': (
                f'{actual_work_hours}h × 時薪${hourly_rate}'
                + (f'（{len(punch_details)}天出勤）' if punch_details else '')
            ),
        })
        allowance_total += hourly_base_pay

        if insured_salary == 0:
            insured_salary = round(hourly_rate * daily_hours * 30, 0)

        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            salary_items_rows = conn.execute(f"""
                SELECT * FROM salary_items
                WHERE active=TRUE AND id IN ({placeholders})
                  AND item_type='deduction'
                  AND (formula LIKE '%%insured_salary%%' OR formula LIKE '%%base_salary%%')
                ORDER BY sort_order, id
            """, staff_item_ids).fetchall()
        else:
            salary_items_rows = conn.execute("""
                SELECT * FROM salary_items
                WHERE active=TRUE
                  AND item_type='deduction'
                  AND (formula LIKE '%%insured_salary%%' OR formula LIKE '%%base_salary%%')
                ORDER BY sort_order, id
            """).fetchall()
        for it in salary_items_rows:
            calc_amt = _eval_formula(it['formula'] or '', base_salary or insured_salary,
                                     insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else (it['formula'] or '')
            items.append({
                'id': it['id'], 'name': it['name'], 'type': 'deduction',
                'amount': round(amt, 2), 'formula': it['formula'] or '',
                'calc_note': note,
            })
            deduction_total += amt

    else:
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            items_rows = conn.execute(
                f"SELECT * FROM salary_items WHERE active=TRUE AND id IN ({placeholders}) ORDER BY sort_order, id",
                staff_item_ids
            ).fetchall()
        else:
            items_rows = conn.execute(
                "SELECT * FROM salary_items WHERE active=TRUE ORDER BY sort_order, id"
            ).fetchall()
        for it in items_rows:
            formula  = it['formula'] or ''
            calc_amt = float(it['amount'] or 0)
            if formula:
                calc_amt = _eval_formula(formula, base_salary, insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else formula
            items.append({
                'id':        it['id'],
                'name':      it['name'],
                'type':      it['item_type'],
                'amount':    round(amt, 2),
                'formula':   formula,
                'calc_note': note,
            })
            if it['item_type'] == 'allowance':
                allowance_total += amt
            else:
                deduction_total += amt

    if ot_pay > 0:
        items.append({
            'id': 'ot', 'name': '加班費（申請）', 'type': 'allowance',
            'amount': round(ot_pay, 2), 'formula': '',
            'calc_note': '核准加班費合計',
        })
        allowance_total += ot_pay

    if unpaid_days > 0 and daily_wage > 0:
        leave_names = '、'.join(set(
            r['leave_name'] for r in leave_rows if float(r['pay_rate']) == 0
        ))
        deduct = round(daily_wage * unpaid_days, 2)
        items.append({
            'id': 'unpaid', 'name': f'無薪假扣款（{leave_names}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{unpaid_days}天 × 日薪${round(daily_wage, 0)}',
        })
        deduction_total += deduct

    if half_pay_rows and daily_wage > 0:
        total_half_deduct = 0.0
        notes = []
        name_set = set()
        for days_here, pay_r, lname in half_pay_rows:
            deduct_rate = round(1.0 - pay_r, 4)
            d = round(daily_wage * days_here * deduct_rate, 2)
            total_half_deduct += d
            name_set.add(lname)
            notes.append(f'{lname} {days_here}天×扣{round(deduct_rate*100,0):.0f}%')
        deduct = round(total_half_deduct, 2)
        items.append({
            'id': 'halfpay', 'name': f'部分薪假扣款（{"、".join(name_set)}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': '，'.join(notes) + f'（日薪${round(daily_wage, 0)}）',
        })
        deduction_total += deduct

    absent_days = 0
    if salary_type == 'monthly' and scheduled_dates and daily_wage > 0:
        punch_rows = conn.execute("""
            SELECT DISTINCT (punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date
            FROM punch_records WHERE staff_id=%s
              AND TO_CHAR(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
        """, (staff['id'], month)).fetchall()
        punched_dates = {
            r['work_date'].isoformat() if hasattr(r['work_date'], 'isoformat') else str(r['work_date'])
            for r in punch_rows
        }
        leave_date_set = set()
        for _lr in leave_rows:
            _ld = _lr['start_date']
            _le = _lr['end_date']
            if hasattr(_ld, 'date'): _ld = _ld.date()
            else: _ld = _d.fromisoformat(str(_ld))
            if hasattr(_le, 'date'): _le = _le.date()
            else: _le = _d.fromisoformat(str(_le))
            while _ld <= _le:
                leave_date_set.add(_ld.isoformat())
                _ld += _td(days=1)
        absent_date_list = sorted(
            ds for ds in scheduled_dates
            if ds not in punched_dates and ds not in leave_date_set
               and _d.fromisoformat(ds) < _today
        )
        absent_days = len(absent_date_list)
        if absent_days > 0:
            deduct = round(daily_wage * absent_days, 2)
            sample = '、'.join(absent_date_list[:3]) + ('等' if absent_days > 3 else '')
            items.append({
                'id': 'absent', 'name': f'缺勤扣款（{absent_days} 天）', 'type': 'deduction',
                'amount': deduct, 'formula': '',
                'calc_note': f'{absent_days} 天 × 日薪 ${round(daily_wage, 0)}（{sample}）',
            })
            deduction_total += deduct

    net_pay = round(allowance_total - deduction_total, 2)

    PAY_LABEL = {1.0: '全薪', 0.5: '半薪', 0.0: '無薪'}
    leave_details = []
    for r in leave_rows:
        d5 = _leave_days_in_month(r)
        if d5 <= 0:
            continue
        pr = float(r['pay_rate'])
        leave_details.append({
            'leave_name': r['leave_name'],
            'days':       d5,
            'pay_rate':   pr,
            'pay_label':  PAY_LABEL.get(pr, f'{int(pr*100)}%薪'),
            'start_date': str(r['start_date']),
            'end_date':   str(r['end_date']),
        })

    holiday_rows5 = conn.execute("""
        SELECT date, name FROM public_holidays
        WHERE TO_CHAR(date,'YYYY-MM')=%s ORDER BY date
    """, (month,)).fetchall()
    holiday_dates_list = [
        {'date': str(r['date']), 'name': r['name']} for r in holiday_rows5
    ]

    return {
        'staff_id':           staff['id'],
        'month':              month,
        'salary_type':        salary_type,
        'base_salary':        base_salary if salary_type == 'monthly' else hourly_base_pay,
        'hourly_rate':        hourly_rate if salary_type == 'hourly' else 0,
        'hourly_base_pay':    hourly_base_pay if salary_type == 'hourly' else 0,
        'actual_work_hours':  actual_work_hours if salary_type == 'hourly' else 0,
        'insured_salary':     insured_salary,
        'work_days':          total_work_days,
        'actual_days':        max(0, actual_days - absent_days),
        'leave_days':         leave_days,
        'unpaid_days':        unpaid_days,
        'absent_days':        absent_days,
        'ot_pay':             ot_pay,
        'allowance_total':    round(allowance_total, 2),
        'deduction_total':    round(deduction_total, 2),
        'net_pay':            net_pay,
        'items':              items,
        'punch_details':      punch_details,
        'leave_details':      leave_details,
        'holiday_dates':      holiday_dates_list,
        'status':             'draft',
    }


def _leave_detail_for_month(conn, staff_id, month):
    """Helper used by my-payslip and record GET to build leave + holiday details."""
    from datetime import date as _d, timedelta as _td
    import calendar as _cal
    y, m = int(month[:4]), int(month[5:])
    mf = _d(y, m, 1)
    ml = _d(y, m, _cal.monthrange(y, m)[1])
    lv_rows = conn.execute("""
        SELECT lr.total_days, lt.pay_rate, lt.name as leave_name,
               lr.start_date, lr.end_date
        FROM leave_requests lr
        JOIN leave_types lt ON lt.id=lr.leave_type_id
        WHERE lr.staff_id=%s AND lr.status='approved'
          AND lr.start_date<=%s AND lr.end_date>=%s
    """, (staff_id, ml, mf)).fetchall()
    PAY_LBL = {1.0: '全薪', 0.5: '半薪', 0.0: '無薪'}
    details = []
    for lr in lv_rows:
        sd = lr['start_date'] if isinstance(lr['start_date'], _d) else _d.fromisoformat(str(lr['start_date']))
        ed = lr['end_date']   if isinstance(lr['end_date'],   _d) else _d.fromisoformat(str(lr['end_date']))
        if sd >= mf and ed <= ml:
            d5 = float(lr['total_days'])
        else:
            sd2 = max(sd, mf); ed2 = min(ed, ml)
            d5 = sum(1 for i in range((ed2 - sd2).days + 1)
                     if (sd2 + _td(days=i)).weekday() != 6)
        if d5 <= 0:
            continue
        pr = float(lr['pay_rate'])
        details.append({
            'leave_name': lr['leave_name'], 'days': d5, 'pay_rate': pr,
            'pay_label':  PAY_LBL.get(pr, f'{int(pr*100)}%薪'),
            'start_date': str(lr['start_date']), 'end_date': str(lr['end_date']),
        })
    hol_rows = conn.execute("""
        SELECT date, name FROM public_holidays
        WHERE TO_CHAR(date,'YYYY-MM')=%s ORDER BY date
    """, (month,)).fetchall()
    holiday_dates = [{'date': str(r['date']), 'name': r['name']} for r in hol_rows]
    return details, holiday_dates


# ── Employee ──────────────────────────────────────────────────────────────────

@bp.route('/api/salary/my-payslip', methods=['GET'])
def api_my_payslip():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _d
        month = _d.today().strftime('%Y-%m')
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.salary_type, ps.hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.staff_id = %s AND sr.month = %s
        """, (sid, month)).fetchone()
        if not row:
            return jsonify({'error': f'{month} 尚無薪資記錄，請聯絡管理員'}), 404
        _st = row['salary_type'] or 'monthly'
        _awk = 0.0; _pd = []
        if _st == 'hourly':
            from blueprints.punch import _calc_punch_hours
            _awk, _, _pd = _calc_punch_hours(conn, sid, month)
        lv_details, hol_list = _leave_detail_for_month(conn, sid, month)
    d = salary_record_row(row)
    d['staff_name']        = row['staff_name']
    d['staff_role']        = row['staff_role']
    d['employee_code']     = row['employee_code'] or ''
    d['department']        = row['department'] or ''
    d['salary_type']       = _st
    d['hourly_rate']       = float(row['hourly_rate'] or 0)
    d['actual_work_hours'] = _awk
    d['punch_details']     = _pd
    d['leave_details']     = lv_details
    d['holiday_dates']     = hol_list
    return jsonify(d)


# ── Admin: Salary Items CRUD ──────────────────────────────────────────────────

@bp.route('/api/salary/items', methods=['GET'])
@require_module('salary')
def api_salary_items_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM salary_items ORDER BY sort_order, id").fetchall()
    return jsonify([salary_item_row(r) for r in rows])


@bp.route('/api/salary/items', methods=['POST'])
@require_module('salary')
def api_salary_item_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO salary_items (name, item_type, formula, amount, description, color, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b.get('item_type', 'allowance'), b.get('formula', ''),
              float(b.get('amount', 0)), b.get('description', ''),
              b.get('color', '#4a7bda'), int(b.get('sort_order', 0)))).fetchone()
    return jsonify(salary_item_row(row)), 201


@bp.route('/api/salary/items/<int:iid>', methods=['PUT'])
@require_module('salary')
def api_salary_item_update(iid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_items SET name=%s, item_type=%s, formula=%s, amount=%s,
              description=%s, color=%s, sort_order=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b.get('item_type', 'allowance'), b.get('formula', ''),
              float(b.get('amount', 0)), b.get('description', ''),
              b.get('color', '#4a7bda'), int(b.get('sort_order', 0)),
              bool(b.get('active', True)), iid)).fetchone()
    return jsonify(salary_item_row(row)) if row else ('', 404)


@bp.route('/api/salary/items/<int:iid>', methods=['DELETE'])
@require_module('salary')
def api_salary_item_delete(iid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_items WHERE id=%s", (iid,))
    return jsonify({'deleted': iid})


# ── Admin: Salary Records ─────────────────────────────────────────────────────

@bp.route('/api/salary/records', methods=['GET'])
@require_module('salary')
def api_salary_records_list():
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _d
        month = _d.today().strftime('%Y-%m')
    result = []
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department,
                   ps.salary_type as staff_salary_type,
                   ps.hourly_rate as staff_hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.month=%s
            ORDER BY ps.name
        """, (month,)).fetchall()
        for r in rows:
            d = salary_record_row(r)
            d['staff_name']    = r['staff_name']
            d['staff_role']    = r['staff_role']
            d['employee_code'] = r['employee_code'] or ''
            d['department']    = r['department'] or ''
            if not d.get('salary_type'): d['salary_type'] = r['staff_salary_type'] or 'monthly'
            if not d.get('hourly_rate'): d['hourly_rate']  = float(r['staff_hourly_rate'] or 0)
            lv_details, holiday_dates = _leave_detail_for_month(conn, r['staff_id'], month)
            d['leave_details'] = lv_details
            d['holiday_dates'] = holiday_dates
            result.append(d)
    return jsonify(result)


@bp.route('/api/salary/records/preview', methods=['POST'])
@require_module('salary')
def api_salary_preview():
    """預覽薪資計算結果（不儲存）"""
    b     = request.get_json(force=True) or {}
    month = b.get('month', '').strip()
    if not month:
        return jsonify({'error': '請指定月份'}), 400
    result = []
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        for staff in staff_list:
            data = _auto_generate_salary(conn, dict(staff), month)
            punch_days = conn.execute("""
                SELECT COUNT(DISTINCT punched_at::date) AS n
                FROM punch_records WHERE staff_id=%s
                  AND to_char(punched_at,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()['n']
            approved_ot = conn.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(ot_hours),0) AS hrs
                FROM overtime_requests WHERE staff_id=%s
                  AND status='approved'
                  AND to_char(request_date,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()
            result.append({
                'staff_id':        data['staff_id'],
                'staff_name':      staff['name'],
                'department':      staff['department'],
                'salary_type':     staff['salary_type'],
                'punch_days':      punch_days,
                'work_days':       float(data['work_days']),
                'actual_days':     float(data['actual_days']),
                'leave_days':      float(data['leave_days']),
                'unpaid_days':     float(data['unpaid_days']),
                'ot_count':        int(approved_ot['n']),
                'ot_hours':        float(approved_ot['hrs']),
                'ot_pay':          float(data['ot_pay']),
                'base_salary':     float(data['base_salary']),
                'allowance_total': float(data['allowance_total']),
                'deduction_total': float(data['deduction_total']),
                'net_pay':         float(data['net_pay']),
                'leave_details':   data['leave_details'],
                'holiday_dates':   data['holiday_dates'],
            })
    return jsonify({'ok': True, 'month': month, 'records': result})


@bp.route('/api/salary/records/generate', methods=['POST'])
@require_module('salary')
def api_salary_generate():
    import calendar as _cal
    from datetime import date as _d
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    if not month: return jsonify({'error': '請指定月份'}), 400
    try:
        year2, mo2 = map(int, month.split('-'))
    except (ValueError, AttributeError):
        return jsonify({'error': '月份格式錯誤，請使用 YYYY-MM'}), 400

    cfg     = _get_salary_config()
    pay_day = cfg['pay_day']
    pay_year, pay_mo = (year2, mo2 + 1) if mo2 < 12 else (year2 + 1, 1)
    effective_pay_day = min(pay_day, _cal.monthrange(pay_year, pay_mo)[1])
    pay_date_str = _d(pay_year, pay_mo, effective_pay_day).isoformat()

    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE"
        ).fetchall()
        generated = 0
        for staff in staff_list:
            data       = _auto_generate_salary(conn, dict(staff), month)
            items_json = _json.dumps(data['items'], ensure_ascii=False)
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
    return jsonify({'ok': True, 'generated': generated, 'month': month, 'pay_date': pay_date_str})


@bp.route('/api/salary/records/<int:rid>', methods=['GET'])
@require_module('salary')
def api_salary_record_get(rid):
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.hire_date,
                   ps.salary_type as staff_salary_type,
                   ps.hourly_rate as staff_hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.id=%s
        """, (rid,)).fetchone()
        if not row: return ('', 404)
        _st    = row.get('staff_salary_type') or 'monthly'
        _month = row['month']
        _awk   = 0.0; _pd = []
        if _st == 'hourly':
            from blueprints.punch import _calc_punch_hours
            _awk, _, _pd = _calc_punch_hours(conn, row['staff_id'], _month)
        lv_details, holiday_dates = _leave_detail_for_month(conn, row['staff_id'], _month)
    d = salary_record_row(row)
    d['staff_name']        = row['staff_name']
    d['staff_role']        = row['staff_role']
    d['employee_code']     = row['employee_code'] or ''
    d['department']        = row['department'] or ''
    d['hire_date']         = row['hire_date'].isoformat() if row['hire_date'] else ''
    if not d.get('salary_type'): d['salary_type'] = _st
    if not d.get('hourly_rate'): d['hourly_rate']  = float(row['staff_hourly_rate'] or 0)
    d['actual_work_hours'] = _awk
    d['punch_details']     = _pd
    d['leave_details']     = lv_details
    d['holiday_dates']     = holiday_dates
    return jsonify(d)


@bp.route('/api/salary/records/<int:rid>', methods=['PUT'])
@require_module('salary')
def api_salary_record_update(rid):
    b          = request.get_json(force=True)
    items_json = _json.dumps(b.get('items', []), ensure_ascii=False)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET
              allowance_total=%s, deduction_total=%s, net_pay=%s,
              items=%s::jsonb, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('allowance_total', 0)), float(b.get('deduction_total', 0)),
              float(b.get('net_pay', 0)), items_json,
              b.get('note', ''), rid)).fetchone()
    return jsonify(salary_record_row(row)) if row else ('', 404)


@bp.route('/api/salary/records/confirm-all', methods=['POST'])
@require_module('salary')
def api_salary_confirm_all():
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    by    = b.get('confirmed_by', '管理員')
    if not month: return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        rows = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE month=%s AND status='draft'
            RETURNING id, staff_id, month, net_pay
        """, (by, month)).fetchall()
    confirmed = len(rows)
    for row in rows:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify({'ok': True, 'confirmed': confirmed})


@bp.route('/api/salary/records/<int:rid>/confirm', methods=['POST'])
@require_module('salary')
def api_salary_confirm(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b.get('confirmed_by', '管理員'), rid)).fetchone()
    if row:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify(salary_record_row(row)) if row else ('', 404)


@bp.route('/api/salary/records/<int:rid>', methods=['DELETE'])
@require_module('salary')
def api_salary_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


# ── Admin: Staff Salary Settings ──────────────────────────────────────────────

@bp.route('/api/salary/staff', methods=['GET'])
@require_module('salary')
def api_salary_staff_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, name, username, role, active, employee_code, department,
                   position_title, hire_date, birth_date, base_salary, insured_salary,
                   daily_hours, ot_rate1, ot_rate2, salary_type, hourly_rate,
                   vacation_quota, salary_notes, salary_item_ids, salary_item_overrides,
                   national_id, gender, insurance_type, address
            FROM punch_staff ORDER BY name
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in ['base_salary', 'insured_salary', 'daily_hours', 'ot_rate1', 'ot_rate2', 'hourly_rate']:
            if d.get(f) is not None: d[f] = float(d[f])
        if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
        if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
        d['annual_leave_days'] = _calc_annual_leave_days(d.get('hire_date'))
        d['service_years']     = _calc_service_years(d.get('hire_date'))
        result.append(d)
    return jsonify(result)


@bp.route('/api/salary/staff/<int:sid>', methods=['PUT'])
@require_module('salary')
def api_salary_staff_update(sid):
    from auth import punch_staff_row
    b = request.get_json(force=True)
    def _f(k, default=0): return float(b.get(k, default) or default)
    def _s(k): return b.get(k, '').strip() if b.get(k) else None
    with get_db() as conn:
        conn.execute("SELECT id FROM punch_staff WHERE id=%s FOR UPDATE", (sid,))
        salary_item_ids      = b.get('salary_item_ids')
        salary_item_ids_json = _json.dumps(salary_item_ids) if salary_item_ids is not None else None
        overrides            = b.get('salary_item_overrides')
        overrides_json       = _json.dumps(overrides) if overrides else None
        conn.execute("""
            UPDATE punch_staff SET
              employee_code=%s, department=%s, position_title=%s,
              hire_date=%s, birth_date=%s,
              base_salary=%s, insured_salary=%s, daily_hours=%s,
              ot_rate1=%s, ot_rate2=%s, salary_type=%s,
              hourly_rate=%s, vacation_quota=%s, salary_notes=%s,
              salary_item_ids=%s, salary_item_overrides=%s,
              national_id=%s, gender=%s, insurance_type=%s, address=%s
            WHERE id=%s
        """, (_s('employee_code'), _s('department'), _s('position_title'),
              _s('hire_date'), _s('birth_date'),
              _f('base_salary'), _f('insured_salary'), _f('daily_hours') or 8,
              _f('ot_rate1') or 1.33, _f('ot_rate2') or 1.67,
              b.get('salary_type', 'monthly'),
              _f('hourly_rate'), b.get('vacation_quota') or None,
              b.get('salary_notes', ''), salary_item_ids_json, overrides_json,
              (b.get('national_id') or '').strip(),
              (b.get('gender') or '').strip(),
              (b.get('insurance_type') or 'regular').strip(),
              (b.get('address') or '').strip(),
              sid))
        row = conn.execute("SELECT * FROM punch_staff WHERE id=%s", (sid,)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)


# ── Admin: Salary Config ──────────────────────────────────────────────────────

@bp.route('/api/salary/config', methods=['GET'])
@require_module('salary')
def api_salary_config_get():
    return jsonify(_get_salary_config())


@bp.route('/api/salary/config', methods=['PUT'])
@require_module('salary')
def api_salary_config_put():
    b              = request.get_json(force=True)
    settlement_day = int(b.get('settlement_day', 1))
    pay_day        = int(b.get('pay_day', 5))
    if not (1 <= settlement_day <= 28):
        return jsonify({'error': '結算日需在 1–28 之間'}), 400
    if not (1 <= pay_day <= 28):
        return jsonify({'error': '發薪日需在 1–28 之間'}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE salary_config SET settlement_day=%s, pay_day=%s, updated_at=NOW() WHERE id=1",
            (settlement_day, pay_day)
        )
    return jsonify({'ok': True, 'settlement_day': settlement_day, 'pay_day': pay_day})


# ── Salary PDF (HTML for print-to-PDF) ───────────────────────────────────────

@bp.route('/api/salary/records/<int:rid>/pdf', methods=['GET'])
@require_module('salary')
def api_salary_pdf(rid):
    if not session.get('logged_in'):
        sid = session.get('punch_staff_id')
        if not sid:
            return '未登入', 401
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.employee_code,
                   ps.department, ps.role, ps.salary_type,
                   ps.hourly_rate, ps.hire_date
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.id = %s
        """, (rid,)).fetchone()
        if not row:
            return '找不到薪資記錄', 404
        _pdf_punch_details = []
        if row.get('salary_type') == 'hourly':
            from blueprints.punch import _calc_punch_hours
            _, _, _pdf_punch_details = _calc_punch_hours(conn, row['staff_id'], row['month'])

    if not session.get('logged_in'):
        if row['staff_id'] != session.get('punch_staff_id'):
            return '無權限', 403

    d            = salary_record_row(row)
    items        = d.get('items') or []
    allow_items  = [i for i in items if i.get('type') == 'allowance']
    deduct_items = [i for i in items if i.get('type') == 'deduction']
    is_hourly    = (row['salary_type'] == 'hourly')

    def money(v):
        try: return f"${float(v):,.0f}"
        except: return '$0'

    def esc_h(s):
        return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    allow_rows = ''.join(f"""
        <tr>
          <td>{esc_h(i['name'])}</td>
          <td class="num green">{money(i['amount'])}</td>
          <td class="note">{esc_h(i.get('calc_note',''))}</td>
        </tr>""" for i in allow_items)

    deduct_rows = ''.join(f"""
        <tr>
          <td>{esc_h(i['name'])}</td>
          <td class="num red">-{money(i['amount'])}</td>
          <td class="note">{esc_h(i.get('calc_note',''))}</td>
        </tr>""" for i in deduct_items)

    punch_table = ''
    if is_hourly and _pdf_punch_details:
        punch_rows = ''.join(f"""
            <tr>
              <td>{p['date']}</td>
              <td>{p['clock_in']}</td>
              <td>{p['clock_out']}</td>
              <td>{p.get('break_mins',0)} min</td>
              <td class="num">{p['net_hours']} h</td>
            </tr>""" for p in _pdf_punch_details)
        punch_table = f"""
        <h3>每日工時明細</h3>
        <table>
          <thead><tr><th>日期</th><th>上班</th><th>下班</th><th>休息</th><th>工時</th></tr></thead>
          <tbody>{punch_rows}</tbody>
          <tfoot><tr><td colspan="4"><strong>合計</strong></td>
            <td class="num"><strong>{d.get('actual_work_hours', 0)} h</strong></td></tr></tfoot>
        </table>"""

    status_str = '已確認' if row['status'] == 'confirmed' else '草稿（未確認）'
    sal_type   = '時薪制' if is_hourly else '月薪制'
    attend_str = (f"實際工時 {d.get('actual_work_hours',0)}h × 時薪 ${float(row['hourly_rate'] or 0):,.0f}"
                  if is_hourly else
                  f"出勤 {d.get('actual_days',0)} 天 / 工作日 {d.get('work_days',0)} 天")
    if float(d.get('leave_days', 0)) > 0:
        attend_str += f"，請假 {d.get('leave_days',0)} 天"
    if float(d.get('unpaid_days', 0)) > 0:
        attend_str += f"（無薪 {d.get('unpaid_days',0)} 天）"

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>薪資單 {esc_h(row['staff_name'])} {esc_h(row['month'])}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Noto Sans TC', 'PingFang TC', 'Microsoft JhengHei', sans-serif;
          font-size: 13px; color: #1a2340; background: #fff; padding: 32px; }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start;
             border-bottom: 3px solid #1a2340; padding-bottom: 16px; margin-bottom: 24px; }}
  .company {{ font-size: 20px; font-weight: 800; color: #1a2340; }}
  .slip-title {{ font-size: 14px; color: #666; margin-top: 4px; }}
  .staff-info {{ font-size: 12px; color: #444; text-align: right; line-height: 1.8; }}
  .summary {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-bottom: 24px; }}
  .sum-card {{ border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 12px 16px; text-align: center; }}
  .sum-label {{ font-size: 10px; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .06em; }}
  .sum-val {{ font-size: 22px; font-weight: 800; font-family: 'DM Mono', monospace; }}
  .sum-val.green {{ color: #2e9e6b; }}
  .sum-val.red   {{ color: #d64242; }}
  .sum-val.navy  {{ color: #1a2340; }}
  .attend {{ background: #f8fafc; border-radius: 6px; padding: 8px 14px;
             font-size: 12px; color: #666; margin-bottom: 20px; }}
  h3 {{ font-size: 12px; font-weight: 700; color: #888; letter-spacing: .08em;
        text-transform: uppercase; margin: 20px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f1f5f9; padding: 8px 12px; text-align: left;
        font-size: 11px; font-weight: 700; color: #666;
        border-bottom: 2px solid #e2e8f0; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #f0f2f8; }}
  td.num {{ text-align: right; font-family: 'DM Mono', monospace; font-weight: 600; }}
  td.note {{ font-size: 11px; color: #999; }}
  td.green {{ color: #2e9e6b; }}
  td.red   {{ color: #d64242; }}
  tfoot td {{ font-weight: 700; background: #f8fafc; border-top: 2px solid #e2e8f0; }}
  .net-row td {{ font-size: 16px; font-weight: 800; background: #1a2340; color: #fff; }}
  .net-row td.num {{ color: #f0c040; font-size: 20px; }}
  .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #e2e8f0;
             display: flex; justify-content: space-between; font-size: 11px; color: #999; }}
  .sign-area {{ display: flex; gap: 48px; margin-top: 40px; }}
  .sign-box {{ flex: 1; border-top: 1px solid #ccc; padding-top: 6px; font-size: 11px; color: #666; }}
  @media print {{
    body {{ padding: 16px; }}
    @page {{ margin: 12mm; size: A4; }}
    .no-print {{ display: none !important; }}
  }}
</style>
</head>
<body>

<div class="no-print" style="text-align:right;margin-bottom:20px">
  <button onclick="window.print()"
    style="padding:10px 24px;background:#1a2340;color:#fff;border:none;border-radius:6px;
           font-size:13px;font-weight:700;cursor:pointer">列印 / 儲存 PDF</button>
</div>

<div class="header">
  <div>
    <div class="company">薪資明細單</div>
    <div class="slip-title">{esc_h(row['month'])} · {sal_type}</div>
  </div>
  <div class="staff-info">
    <div><strong>{esc_h(row['staff_name'])}</strong></div>
    <div>{esc_h(row['employee_code'] or '')}　{esc_h(row['department'] or '')}　{esc_h(row['role'] or '')}</div>
    <div>到職日：{esc_h(str(row['hire_date']) if row['hire_date'] else '—')}</div>
    <div>發薪日：<strong>{esc_h(str(d.get('pay_date','')) or '—')}</strong></div>
    <div>狀態：<strong>{status_str}</strong></div>
  </div>
</div>

<div class="summary">
  <div class="sum-card">
    <div class="sum-label">津貼合計</div>
    <div class="sum-val green">{money(d.get('allowance_total',0))}</div>
  </div>
  <div class="sum-card">
    <div class="sum-label">扣除合計</div>
    <div class="sum-val red">-{money(d.get('deduction_total',0))}</div>
  </div>
  <div class="sum-card" style="border-color:#1a2340">
    <div class="sum-label">實領金額</div>
    <div class="sum-val navy">{money(d.get('net_pay',0))}</div>
  </div>
</div>

<div class="attend">{attend_str}</div>

<h3>津貼項目</h3>
<table>
  <thead><tr><th>項目</th><th style="text-align:right">金額</th><th>計算說明</th></tr></thead>
  <tbody>{allow_rows}</tbody>
  <tfoot>
    <tr><td><strong>津貼合計</strong></td>
        <td class="num green"><strong>{money(d.get('allowance_total',0))}</strong></td><td></td></tr>
  </tfoot>
</table>

<h3>扣除項目</h3>
<table>
  <thead><tr><th>項目</th><th style="text-align:right">金額</th><th>計算說明</th></tr></thead>
  <tbody>{deduct_rows if deduct_rows else '<tr><td colspan="3" style="color:#ccc;text-align:center;padding:12px">無扣除項目</td></tr>'}</tbody>
  <tfoot>
    <tr><td><strong>扣除合計</strong></td>
        <td class="num red"><strong>-{money(d.get('deduction_total',0))}</strong></td><td></td></tr>
  </tfoot>
</table>

<table style="margin-top:12px">
  <tbody>
    <tr class="net-row">
      <td><strong>實領金額</strong></td>
      <td class="num">{money(d.get('net_pay',0))}</td>
      <td style="color:#ccc;font-size:11px">= 津貼 {money(d.get('allowance_total',0))} - 扣除 {money(d.get('deduction_total',0))}</td>
    </tr>
  </tbody>
</table>

{punch_table}

<div class="sign-area">
  <div class="sign-box">員工簽名</div>
  <div class="sign-box">主管確認</div>
  <div class="sign-box">人資確認</div>
</div>

<div class="footer">
  <span>本薪資單由系統自動產生</span>
  <span>列印日期：<script>document.write(new Date().toLocaleDateString('zh-TW'))</script></span>
</div>

</body>
</html>"""

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}
