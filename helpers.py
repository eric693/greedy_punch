"""Shared helpers used across multiple blueprints."""
import json as _json
import math
from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz

from config import TW_TZ


# ─── GPS ──────────────────────────────────────────────────────────────────────

def _gps_distance(lat1, lng1, lat2, lng2):
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return int(2 * R * math.asin(math.sqrt(a)))


# ─── DateTime ─────────────────────────────────────────────────────────────────

def _parse_tw_datetime(s):
    """Parse a datetime string treating naive strings as Taiwan time (UTC+8)."""
    if not s:
        return None
    dt = _dt.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


# ─── Row Formatters ───────────────────────────────────────────────────────────

def punch_staff_row(row):
    if not row: return None
    d = dict(row)
    d.pop('password_hash', None)
    if d.get('password_plain') is None: d['password_plain'] = ''
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
    if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
    return d


def punch_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['latitude', 'longitude']:
        if d.get(f) is not None: d[f] = float(d[f])
    for f in ['punched_at', 'created_at']:
        if d.get(f):
            dt = d[f]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            d[f] = dt.astimezone(TW_TZ).isoformat()
    return d


def loc_row(row):
    if not row: return None
    d = dict(row)
    for f in ['lat', 'lng']:
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d


def punch_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('requested_at'): d['requested_at'] = d['requested_at'].isoformat()
    if d.get('reviewed_at'):  d['reviewed_at']  = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    return d


def ot_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('request_date'): d['request_date'] = d['request_date'].isoformat()
    if d.get('start_time'):   d['start_time']   = str(d['start_time'])[:5]
    if d.get('end_time'):     d['end_time']      = str(d['end_time'])[:5]
    if d.get('ot_pay'):       d['ot_pay']        = float(d['ot_pay'])
    if d.get('ot_hours'):     d['ot_hours']      = float(d['ot_hours'])
    if d.get('reviewed_at'):  d['reviewed_at']   = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']    = d['created_at'].isoformat()
    return d


def shift_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_time'): d['start_time'] = str(d['start_time'])[:5]
    if d.get('end_time'):   d['end_time']   = str(d['end_time'])[:5]
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def shift_assign_row(row):
    if not row: return None
    d = dict(row)
    if d.get('shift_date'): d['shift_date'] = d['shift_date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def sched_req_row(row):
    if not row: return None
    d = dict(row)
    if isinstance(d.get('dates'), str):
        try: d['dates'] = _json.loads(d['dates'])
        except: d['dates'] = []
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    return d


def leave_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('max_days') is not None: d['max_days'] = float(d['max_days'])
    if d.get('pay_rate') is not None: d['pay_rate'] = float(d['pay_rate'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def leave_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_date'): d['start_date'] = d['start_date'].isoformat()
    if d.get('end_date'):   d['end_date']   = d['end_date'].isoformat()
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    if d.get('total_days') is not None: d['total_days'] = float(d['total_days'])
    return d


def leave_balance_row(row):
    if not row: return None
    d = dict(row)
    if d.get('total_days') is not None: d['total_days'] = float(d['total_days'])
    if d.get('used_days')  is not None: d['used_days']  = float(d['used_days'])
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d


def holiday_row(row):
    if not row: return None
    d = dict(row)
    if d.get('date'):       d['date']       = d['date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


# ─── Schedule Helpers ─────────────────────────────────────────────────────────

def get_schedule_config(conn, month):
    row = conn.execute("SELECT * FROM schedule_config WHERE month=%s", (month,)).fetchone()
    if not row:
        return {'month': month, 'max_off_per_day': 2, 'vacation_quota': 8, 'notes': ''}
    return dict(row)


def get_off_counts(conn, month):
    rows = conn.execute("""
        SELECT elem as d, COUNT(*) as cnt
        FROM schedule_requests,
             jsonb_array_elements_text(dates) as elem
        WHERE month=%s AND status IN ('approved','pending')
        GROUP BY elem
    """, (month,)).fetchall()
    return {r['d']: int(r['cnt']) for r in rows}


# ─── Holiday Check ────────────────────────────────────────────────────────────

def _is_holiday(conn, date_str):
    row = conn.execute(
        "SELECT id FROM public_holidays WHERE date=%s", (date_str,)
    ).fetchone()
    return row is not None


# ─── Annual Leave Calculations ────────────────────────────────────────────────

def _calc_annual_leave_days(hire_date_str, ref_date_str=None):
    """勞基法第38條特休天數計算（2017年修正版）"""
    if not hire_date_str:
        return 0
    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return 0

    ref = _date.today()
    if ref_date_str:
        try:
            ref = _date.fromisoformat(str(ref_date_str))
        except Exception:
            pass

    months = (ref.year - hire.year) * 12 + (ref.month - hire.month)
    if ref.day < hire.day:
        months -= 1
    if months < 0:
        months = 0

    years_complete = months // 12

    if months < 6:
        return 0
    elif months < 12:
        return 3
    elif years_complete < 2:
        return 7
    elif years_complete < 3:
        return 10
    elif years_complete < 5:
        return 14
    elif years_complete < 10:
        return 15
    else:
        return min(15 + (years_complete - 9), 30)


def _calc_annual_leave_schedule(hire_date_str):
    """回傳員工特休天數完整排程表，供前端顯示用。"""
    if not hire_date_str:
        return []
    import calendar as _cal
    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return []

    milestones = [6, 12, 24, 36, 60, 120]
    for extra_y in range(1, 21):
        milestones.append(120 + extra_y * 12)

    result = []
    today = _date.today()
    current_days = _calc_annual_leave_days(hire_date_str)

    for months in milestones:
        y, m = divmod(months, 12)
        target_year  = hire.year + y + (1 if hire.month - 1 + m > 12 else 0)
        target_month = (hire.month - 1 + m) % 12 + 1
        last_day     = _cal.monthrange(target_year, target_month)[1]
        target_day   = min(hire.day, last_day)
        reached = _date(target_year, target_month, target_day)
        days = _calc_annual_leave_days(hire_date_str, reached.isoformat())
        is_current = (days == current_days and reached <= today)
        result.append({
            'label':        f'到職滿{months}個月',
            'days':         days,
            'date_reached': reached.isoformat(),
            'is_past':      reached < today,
            'is_current':   is_current,
        })
        if days >= 30:
            break

    return result


def _calc_leave_days(start_date_str, end_date_str, start_half=False, end_half=False):
    """計算請假天數（含半天選項），排除週日"""
    try:
        s = _date.fromisoformat(start_date_str)
        e = _date.fromisoformat(end_date_str)
    except Exception:
        return 0.0
    if e < s: return 0.0
    days = 0.0
    cur  = s
    while cur <= e:
        if cur.weekday() != 6:
            if cur == s and cur == e:
                if start_half and end_half:
                    days += 1.0
                elif start_half or end_half:
                    days += 0.5
                else:
                    days += 1.0
            elif cur == s and start_half:
                days += 0.5
            elif cur == e and end_half:
                days += 0.5
            else:
                days += 1.0
        cur += _td(days=1)
    return days


# ─── LINE Notifications ───────────────────────────────────────────────────────

def _notify_staff_line(staff_id, message):
    """Send LINE notification to a staff member if they have LINE bound."""
    from config import DATABASE_URL
    if not DATABASE_URL:
        return
    try:
        from database import get_db
        with get_db() as conn:
            staff = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE id=%s", (staff_id,)
            ).fetchone()
            if not staff or not staff['line_user_id']:
                return
            cfg = conn.execute(
                "SELECT * FROM line_punch_config WHERE id=1"
            ).fetchone()
        if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
            return
        from linebot import LineBotApi
        from linebot.models import TextSendMessage
        LineBotApi(cfg['channel_access_token']).push_message(
            staff['line_user_id'],
            TextSendMessage(text=message)
        )
    except Exception as e:
        print(f"[LINE notify] staff_id={staff_id}: {e}")


def _notify_review_result(staff_id, category, action, extra_info=''):
    """Send a formatted LINE notification for review results."""
    ACTION_LABEL = {'approved': '核准', 'rejected': '退回', 'confirmed': '確認'}
    ACTION_ICON  = {'approved': '[核准]', 'rejected': '[退回]', 'confirmed': '[確認]'}
    label = ACTION_LABEL.get(action, action)
    icon  = ACTION_ICON.get(action, '')
    msg   = f"{icon} {category}{label}\n{extra_info}\n\n請至員工系統查看詳情。"
    _notify_staff_line(staff_id, msg.strip())


def _broadcast_announcement_line(title, content):
    """廣播公告給所有已綁定 LINE 的在職員工"""
    try:
        from database import get_db
        with get_db() as conn:
            cfg = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
            if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
                return
            staff_rows = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE active=TRUE AND line_user_id IS NOT NULL"
            ).fetchall()
        if not staff_rows:
            return
        from linebot import LineBotApi
        from linebot.models import TextSendMessage
        api = LineBotApi(cfg['channel_access_token'])
        snippet = content[:60] + ('…' if len(content) > 60 else '')
        msg = f"[公告] {title}\n{snippet}\n\n請至員工系統查看完整公告。"
        for s in staff_rows:
            try:
                api.push_message(s['line_user_id'], TextSendMessage(text=msg))
            except Exception as e:
                print(f"[LINE broadcast] {s['line_user_id']}: {e}")
    except Exception as e:
        print(f"[LINE broadcast] error: {e}")
