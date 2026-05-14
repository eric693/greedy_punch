import calendar as _cal
from collections import defaultdict
from datetime import datetime as _dt, timedelta as _td, timezone as _tz, date as _date

import psycopg
from flask import Blueprint, request, jsonify, render_template, session

from auth import login_required, require_module
from config import TW_TZ
from database import get_db, _hash_pw
from helpers import (
    _gps_distance, _parse_tw_datetime,
    punch_staff_row, punch_record_row, loc_row, wifi_network_row, punch_req_row,
    _notify_review_result,
)

bp = Blueprint('punch', __name__)


# ─── Employee Pages ───────────────────────────────────────────────────────────

@bp.route('/punch')
@bp.route('/staff')
def punch_page():
    return render_template('staff.html')


# ─── Employee Session ─────────────────────────────────────────────────────────

@bp.route('/api/punch/login', methods=['POST'])
def api_punch_login():
    b = request.get_json(force=True)
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '請輸入帳號及密碼'}), 400
    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE username=%s AND active=TRUE", (username,)
        ).fetchone()
    if not staff or staff['password_hash'] != _hash_pw(password):
        return jsonify({'error': '帳號或密碼錯誤'}), 401
    session['punch_staff_id']   = staff['id']
    session['punch_staff_name'] = staff['name']
    return jsonify({'id': staff['id'], 'name': staff['name'], 'role': staff['role']})


@bp.route('/api/punch/logout', methods=['POST'])
def api_punch_logout():
    session.pop('punch_staff_id', None)
    session.pop('punch_staff_name', None)
    return jsonify({'ok': True})


@bp.route('/api/punch/me', methods=['GET'])
def api_punch_me():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        staff = conn.execute(
            "SELECT id,name,role FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
    if not staff:
        session.pop('punch_staff_id', None)
        return jsonify({'error': 'not logged in'}), 401
    return jsonify({'id': staff['id'], 'name': staff['name'], 'role': staff['role']})


@bp.route('/api/punch/change-password', methods=['POST'])
def api_punch_change_password():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    b = request.get_json(force=True)
    old_pw = (b.get('old_password') or '').strip()
    new_pw = (b.get('new_password') or '').strip()
    if not old_pw or not new_pw:
        return jsonify({'error': '請填寫舊密碼與新密碼'}), 400
    if len(new_pw) < 4:
        return jsonify({'error': '新密碼至少 4 個字元'}), 400
    with get_db() as conn:
        staff = conn.execute(
            "SELECT id, password_hash FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
        if not staff:
            return jsonify({'error': '帳號不存在'}), 404
        if staff['password_hash'] != _hash_pw(old_pw):
            return jsonify({'error': '舊密碼錯誤'}), 400
        conn.execute(
            "UPDATE punch_staff SET password_hash=%s, password_plain=%s WHERE id=%s",
            (_hash_pw(new_pw), new_pw, sid)
        )
    return jsonify({'ok': True})


# ─── Punch Verification Helpers ───────────────────────────────────────────────

def _get_client_ip(req):
    xff = req.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return req.remote_addr or ''


def _check_wifi_ip(client_ip, wifi_networks):
    for net in wifi_networks:
        if not net.get('active'):
            continue
        prefix = (net.get('ip_prefix') or '').strip()
        if prefix and client_ip.startswith(prefix):
            return True, net['name']
    return False, None


# ─── Punch Settings ────────────────────────────────────────────────────────────

@bp.route('/api/punch/settings', methods=['GET'])
def api_punch_settings_get():
    with get_db() as conn:
        cfg  = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
        locs = conn.execute(
            "SELECT * FROM punch_locations WHERE active=TRUE ORDER BY id"
        ).fetchall()
        wifi = conn.execute(
            "SELECT * FROM punch_wifi_networks WHERE active=TRUE ORDER BY id"
        ).fetchall()
    punch_mode = (cfg['punch_mode'] if cfg and cfg.get('punch_mode') else
                  ('gps' if (cfg and cfg['gps_required']) else 'none'))
    return jsonify({
        'gps_required': cfg['gps_required'] if cfg else False,
        'punch_mode':   punch_mode,
        'locations':    [loc_row(r) for r in locs],
        'wifi_networks': [wifi_network_row(r) for r in wifi],
    })


@bp.route('/api/punch/config', methods=['PUT'])
@login_required
def api_punch_config_update():
    b = request.get_json(force=True)
    punch_mode = b.get('punch_mode', 'none')
    if punch_mode not in ('none', 'gps', 'wifi', 'any'):
        return jsonify({'error': '無效的打卡模式'}), 400
    gps_required = punch_mode in ('gps', 'any')
    with get_db() as conn:
        conn.execute(
            "UPDATE punch_config SET gps_required=%s, punch_mode=%s, updated_at=NOW() WHERE id=1",
            (gps_required, punch_mode)
        )
    return jsonify({'punch_mode': punch_mode, 'gps_required': gps_required})


# ─── WiFi Network CRUD ─────────────────────────────────────────────────────────

@bp.route('/api/punch/wifi-networks', methods=['GET'])
@login_required
def api_wifi_networks_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_wifi_networks ORDER BY id").fetchall()
    return jsonify([wifi_network_row(r) for r in rows])


@bp.route('/api/punch/wifi-networks', methods=['POST'])
@login_required
def api_wifi_networks_create():
    b = request.get_json(force=True)
    name      = b.get('name', '').strip() or 'WiFi 網路'
    ip_prefix = b.get('ip_prefix', '').strip()
    if not ip_prefix:
        return jsonify({'error': '請填入 IP 前綴'}), 400
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO punch_wifi_networks (name, ip_prefix) VALUES (%s,%s) RETURNING *",
            (name, ip_prefix)
        ).fetchone()
    return jsonify(wifi_network_row(row)), 201


@bp.route('/api/punch/wifi-networks/<int:wid>', methods=['PUT'])
@login_required
def api_wifi_networks_update(wid):
    b = request.get_json(force=True)
    name      = b.get('name', '').strip() or 'WiFi 網路'
    ip_prefix = b.get('ip_prefix', '').strip()
    active    = bool(b.get('active', True))
    if not ip_prefix:
        return jsonify({'error': '請填入 IP 前綴'}), 400
    with get_db() as conn:
        row = conn.execute(
            "UPDATE punch_wifi_networks SET name=%s,ip_prefix=%s,active=%s,updated_at=NOW() WHERE id=%s RETURNING *",
            (name, ip_prefix, active, wid)
        ).fetchone()
    return jsonify(wifi_network_row(row)) if row else ('', 404)


@bp.route('/api/punch/wifi-networks/<int:wid>', methods=['DELETE'])
@login_required
def api_wifi_networks_delete(wid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_wifi_networks WHERE id=%s", (wid,))
    return jsonify({'deleted': wid})


@bp.route('/api/punch/check-wifi', methods=['GET'])
def api_punch_check_wifi():
    with get_db() as conn:
        wifi_nets = conn.execute("SELECT * FROM punch_wifi_networks WHERE active=TRUE").fetchall()
    nets = [dict(r) for r in wifi_nets]
    local_ip  = request.args.get('local_ip', '').strip()
    server_ip = _get_client_ip(request)
    matched, name = _check_wifi_ip(local_ip, nets) if local_ip else (False, None)
    if not matched:
        matched, name = _check_wifi_ip(server_ip, nets)
    return jsonify({'matched': matched, 'network_name': name, 'local_ip': local_ip, 'server_ip': server_ip})


@bp.route('/api/punch/my-ip', methods=['GET'])
@login_required
def api_punch_my_ip():
    return jsonify({'ip': _get_client_ip(request)})


@bp.route('/api/punch/locations', methods=['GET'])
@login_required
def api_punch_locations_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_locations ORDER BY id").fetchall()
    return jsonify([loc_row(r) for r in rows])


@bp.route('/api/punch/locations', methods=['POST'])
@login_required
def api_punch_locations_create():
    b = request.get_json(force=True)
    name = b.get('location_name', '').strip() or '打卡地點'
    try:
        lat = float(b['lat']); lng = float(b['lng'])
    except Exception:
        return jsonify({'error': '請填入有效的緯度和經度'}), 400
    radius_m = int(b.get('radius_m') or 100)
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO punch_locations (location_name, lat, lng, radius_m) VALUES (%s,%s,%s,%s) RETURNING *",
            (name, lat, lng, radius_m)
        ).fetchone()
    return jsonify(loc_row(row)), 201


@bp.route('/api/punch/locations/<int:lid>', methods=['PUT'])
@login_required
def api_punch_locations_update(lid):
    b = request.get_json(force=True)
    name = b.get('location_name', '').strip() or '打卡地點'
    try:
        lat = float(b['lat']); lng = float(b['lng'])
    except Exception:
        return jsonify({'error': '請填入有效的緯度和經度'}), 400
    radius_m = int(b.get('radius_m') or 100)
    active   = bool(b.get('active', True))
    with get_db() as conn:
        row = conn.execute(
            "UPDATE punch_locations SET location_name=%s,lat=%s,lng=%s,radius_m=%s,active=%s,updated_at=NOW() WHERE id=%s RETURNING *",
            (name, lat, lng, radius_m, active, lid)
        ).fetchone()
    return jsonify(loc_row(row)) if row else ('', 404)


@bp.route('/api/punch/locations/<int:lid>', methods=['DELETE'])
@login_required
def api_punch_locations_delete(lid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_locations WHERE id=%s", (lid,))
    return jsonify({'deleted': lid})


# ─── Clock In / Out ───────────────────────────────────────────────────────────

@bp.route('/api/punch/clock', methods=['POST'])
def api_punch_clock():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401

    b          = request.get_json(force=True)
    punch_type = b.get('punch_type')
    lat        = b.get('lat')
    lng        = b.get('lng')

    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400

    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
        if not staff:
            return jsonify({'error': '員工不存在'}), 404
        cfg       = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
        locs      = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()
        wifi_nets = conn.execute("SELECT * FROM punch_wifi_networks WHERE active=TRUE").fetchall()

    punch_mode = (cfg['punch_mode'] if cfg and cfg.get('punch_mode') else
                  ('gps' if (cfg and cfg['gps_required']) else 'none'))

    gps_distance = None
    matched_loc  = None
    if lat is not None and lng is not None and locs:
        for loc in locs:
            d = _gps_distance(lat, lng, float(loc['lat']), float(loc['lng']))
            if gps_distance is None or d < gps_distance:
                gps_distance = d
                matched_loc  = loc

    gps_ok = (lat is not None and lng is not None and matched_loc is not None and
              gps_distance is not None and gps_distance <= int(matched_loc['radius_m']))

    nets      = [dict(r) for r in wifi_nets]
    local_ip  = b.get('local_ip', '').strip()
    server_ip = _get_client_ip(request)
    wifi_ok, wifi_net_name = _check_wifi_ip(local_ip, nets) if local_ip else (False, None)
    if not wifi_ok:
        wifi_ok, wifi_net_name = _check_wifi_ip(server_ip, nets)

    if punch_mode == 'gps':
        if lat is None or lng is None:
            return jsonify({'error': '無法取得 GPS，請允許定位權限後重試'}), 403
        if not locs:
            return jsonify({'error': '管理員尚未設定任何打卡地點'}), 403
        if not gps_ok:
            return jsonify({
                'error': f'距離最近地點「{matched_loc["location_name"]}」{gps_distance} 公尺，超出允許範圍（{matched_loc["radius_m"]} 公尺）',
                'distance': gps_distance,
                'radius':   int(matched_loc['radius_m'])
            }), 403
    elif punch_mode == 'wifi':
        if not wifi_nets:
            return jsonify({'error': '管理員尚未設定任何 WiFi 網路'}), 403
        if not wifi_ok:
            return jsonify({'error': '未偵測到公司 WiFi，請連接公司網路後重試'}), 403
    elif punch_mode == 'any':
        if not gps_ok and not wifi_ok:
            if not locs and not wifi_nets:
                return jsonify({'error': '管理員尚未設定任何打卡地點或 WiFi 網路'}), 403
            if gps_distance is not None and matched_loc:
                return jsonify({
                    'error': f'GPS 超出範圍且未偵測到公司 WiFi，請在公司範圍或 WiFi 內打卡',
                    'distance': gps_distance,
                    'radius':   int(matched_loc['radius_m'])
                }), 403
            return jsonify({'error': '無法驗證位置，請在 GPS 範圍或公司 WiFi 下打卡'}), 403

    with get_db() as conn:
        recent = conn.execute("""
            SELECT id FROM punch_records
            WHERE staff_id=%s AND punch_type=%s
              AND punched_at > NOW() - INTERVAL '1 minute'
        """, (sid, punch_type)).fetchone()
        if recent:
            return jsonify({'error': '1 分鐘內已打過卡'}), 429

        if matched_loc:
            matched_name = matched_loc['location_name']
        elif wifi_net_name:
            matched_name = wifi_net_name
        else:
            matched_name = ''
        row = conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, latitude, longitude, gps_distance, location_name)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, punch_type, lat, lng, gps_distance, matched_name)).fetchone()

    d = punch_record_row(row)
    d['staff_name']   = staff['name']
    d['gps_distance'] = gps_distance
    return jsonify(d), 201


@bp.route('/api/punch/today', methods=['GET'])
def api_punch_today():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify([])
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.*, ps.name as staff_name
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE pr.staff_id=%s
              AND (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
                = (NOW() AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY pr.punched_at ASC
        """, (sid,)).fetchall()
    return jsonify([punch_record_row(r) for r in rows])


@bp.route('/api/punch/my-records', methods=['GET'])
def api_punch_my_records():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    month = request.args.get('month', '')
    if not month:
        month = _dt.now(TW_TZ).strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND to_char(punched_at AT TIME ZONE 'Asia/Taipei', 'YYYY-MM') = %s
            ORDER BY punched_at ASC
        """, (sid, month)).fetchall()

        y_mr, m_mr = int(month[:4]), int(month[5:])
        mf_mr = _date(y_mr, m_mr, 1)
        ml_mr = _date(y_mr, m_mr, _cal.monthrange(y_mr, m_mr)[1])
        lv_rows_mr = conn.execute("""
            SELECT lr.start_date, lr.end_date, lr.start_half, lr.end_half,
                   lt.name as leave_name, lt.pay_rate
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s AND lr.status='approved'
              AND lr.start_date<=%s AND lr.end_date>=%s
        """, (sid, ml_mr, mf_mr)).fetchall()

        sh_rows_mr = conn.execute("""
            SELECT sa.shift_date, st.start_time, st.end_time
            FROM shift_assignments sa
            JOIN shift_types st ON st.id=sa.shift_type_id
            WHERE sa.staff_id=%s AND TO_CHAR(sa.shift_date,'YYYY-MM')=%s
        """, (sid, month)).fetchall()

    shifts_by_date = {}
    for sr in sh_rows_mr:
        ds  = sr['shift_date'].isoformat() if hasattr(sr['shift_date'], 'isoformat') else str(sr['shift_date'])
        st_t = sr['start_time']; et_t = sr['end_time']
        shifts_by_date[ds] = {
            'start': str(st_t)[:5],
            'end':   str(et_t)[:5],
            'cross_midnight': et_t < st_t,
        }

    PAY_LBL_MR = {1.0: '全薪', 0.5: '半薪', 0.0: '無薪'}
    leave_by_date = {}
    for _lr in lv_rows_mr:
        _sd = _lr['start_date'] if isinstance(_lr['start_date'], _date) else _date.fromisoformat(str(_lr['start_date']))
        _ed = _lr['end_date']   if isinstance(_lr['end_date'],   _date) else _date.fromisoformat(str(_lr['end_date']))
        cur = max(_sd, mf_mr)
        while cur <= min(_ed, ml_mr):
            ds = cur.isoformat()
            if ds not in leave_by_date:
                leave_by_date[ds] = []
            pr = float(_lr['pay_rate'])
            is_half = (cur == _sd and bool(_lr['start_half'])) or (cur == _ed and bool(_lr['end_half']))
            leave_by_date[ds].append({
                'leave_name': _lr['leave_name'] + ('（半天）' if is_half else ''),
                'pay_label':  PAY_LBL_MR.get(pr, f'{int(pr*100)}%薪'),
                'pay_rate':   pr,
            })
            cur += _td(days=1)

    LABEL = {'in': '上班', 'out': '下班', 'break_out': '休息開始', 'break_in': '休息結束'}
    result = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tz.utc)
        pa_tw    = pa.astimezone(TW_TZ)
        date_str = pa_tw.strftime('%Y-%m-%d')
        time_str = pa_tw.strftime('%H:%M')
        if date_str not in result:
            result[date_str] = []
        result[date_str].append({
            'type':          r['punch_type'],
            'label':         LABEL.get(r['punch_type'], r['punch_type']),
            'time':          time_str,
            'gps_distance':  r['gps_distance'],
            'location_name': r['location_name'] or '',
            'is_manual':     bool(r['is_manual']),
        })
    return jsonify({'month': month, 'records': result, 'leaves': leave_by_date, 'shifts': shifts_by_date})


# ─── Shift Time Helpers ───────────────────────────────────────────────────────

def _build_shift_time_map(conn, month, staff_ids=None):
    from datetime import timedelta as _tdsh, datetime as _dtsh
    where  = "TO_CHAR(sa.shift_date,'YYYY-MM')=%s"
    params = [month]
    if staff_ids:
        ph = ','.join(['%s'] * len(staff_ids))
        where += f" AND sa.staff_id IN ({ph})"
        params.extend(staff_ids)
    sr_rows = conn.execute(f"""
        SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time
        FROM shift_assignments sa
        JOIN shift_types st ON st.id=sa.shift_type_id
        WHERE {where}
    """, params).fetchall()
    shift_map = {}
    for sr in sr_rows:
        sd = sr['shift_date']
        if not hasattr(sd, 'year'):
            sd = _date.fromisoformat(str(sd))
        ds  = sd.isoformat()
        st_t, et_t = sr['start_time'], sr['end_time']
        s_start = _dtsh(sd.year, sd.month, sd.day, st_t.hour, st_t.minute, tzinfo=TW_TZ)
        if et_t < st_t:
            nd    = sd + _tdsh(days=1)
            s_end = _dtsh(nd.year, nd.month, nd.day, et_t.hour, et_t.minute, tzinfo=TW_TZ)
        else:
            s_end = _dtsh(sd.year, sd.month, sd.day, et_t.hour, et_t.minute, tzinfo=TW_TZ)
        shift_map[(sr['staff_id'], ds)] = (s_start, s_end)
    return shift_map


def _clamp_to_shift(actual_start, actual_end, shift_map, staff_id, date_str):
    key = (staff_id, date_str)
    if key not in shift_map:
        return actual_start, actual_end
    s_start, s_end = shift_map[key]
    ws = max(actual_start, s_start)
    we = min(actual_end,   s_end)
    if ws >= we:
        return None, None
    return ws, we


def _shift_aware_day_map(raw_punches, tz):
    result   = defaultdict(lambda: {'ins': [], 'outs': [], 'break_outs': [], 'break_ins': [], 'has_manual': False})
    last_in  = {}
    for r in raw_punches:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tz.utc)
        pa_tw = pa.astimezone(tz)
        sid   = r['staff_id']
        ptype = r['punch_type']
        if ptype == 'in':
            work_date    = pa_tw.date()
            last_in[sid] = pa_tw
        else:
            prev_in = last_in.get(sid)
            work_date = (prev_in.date() if prev_in is not None and 0 < (pa_tw - prev_in).total_seconds() <= 28 * 3600
                         else pa_tw.date())
        key    = (sid, work_date.isoformat())
        bucket = result[key]
        if r.get('is_manual'):
            bucket['has_manual'] = True
        if   ptype == 'in':        bucket['ins'].append(pa_tw)
        elif ptype == 'out':       bucket['outs'].append(pa_tw)
        elif ptype == 'break_out': bucket['break_outs'].append(pa_tw)
        elif ptype == 'break_in':  bucket['break_ins'].append(pa_tw)
    return result


def _calc_punch_hours(conn, staff_id, month):
    rows = conn.execute("""
        SELECT %s as staff_id, punch_type, punched_at, is_manual
        FROM punch_records
        WHERE staff_id=%s
          AND punched_at AT TIME ZONE 'Asia/Taipei' >=
              date_trunc('month', TO_DATE(%s,'YYYY-MM')) - INTERVAL '1 day'
          AND punched_at AT TIME ZONE 'Asia/Taipei' <
              date_trunc('month', TO_DATE(%s,'YYYY-MM')) + INTERVAL '1 month 2 days'
        ORDER BY punched_at ASC
    """, (staff_id, staff_id, month, month)).fetchall()

    shift_map = _build_shift_time_map(conn, month, staff_ids=[staff_id])
    day_map   = _shift_aware_day_map(rows, TW_TZ)

    total_hours = 0.0
    work_days   = 0
    details     = []
    for (sid, ds), bucket in sorted(day_map.items()):
        if not ds.startswith(month):
            continue
        ins  = bucket['ins']
        outs = bucket['outs']
        if not ins or not outs:
            continue
        work_start, work_end = _clamp_to_shift(min(ins), max(outs), shift_map, sid, ds)
        if work_start is None:
            continue
        gross_mins = (work_end - work_start).total_seconds() / 60
        break_mins = 0.0
        for bo in bucket['break_outs']:
            matched = [bi for bi in bucket['break_ins'] if bi > bo]
            if matched:
                break_mins += (min(matched) - bo).total_seconds() / 60
        if gross_mins >= 480:
            break_mins = max(break_mins, 60)
        elif gross_mins >= 240:
            break_mins = max(break_mins, 30)
        net_mins  = max(0, gross_mins - break_mins)
        net_hours = round(net_mins / 60, 2)
        total_hours += net_hours
        work_days   += 1
        details.append({
            'date':        ds,
            'clock_in':    min(ins).strftime('%H:%M'),
            'clock_out':   max(outs).strftime('%H:%M'),
            'break_mins':  int(break_mins),
            'net_hours':   net_hours,
        })
    return round(total_hours, 2), work_days, details


# ─── Admin: Staff CRUD ────────────────────────────────────────────────────────

@bp.route('/api/punch/staff', methods=['GET'])
@login_required
def api_punch_staff_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, name, username, employee_code, department, position_title,
                   role, active, hire_date, birth_date, line_user_id,
                   salary_type, daily_hours, vacation_quota,
                   created_at, password_plain
            FROM punch_staff ORDER BY name
        """).fetchall()
    return jsonify([punch_staff_row(r) for r in rows])


@bp.route('/api/punch/staff', methods=['POST'])
@login_required
def api_punch_staff_create():
    b             = request.get_json(force=True)
    name          = b.get('name', '').strip()
    username      = b.get('username', '').strip()
    password      = b.get('password', '').strip()
    if not name:     return jsonify({'error': '姓名為必填'}), 400
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password or len(password) < 4:
        return jsonify({'error': '密碼至少 4 個字元'}), 400
    employee_code  = (b.get('employee_code') or '').strip() or None
    department     = (b.get('department') or '').strip()
    hire_date      = b.get('hire_date') or None
    birth_date     = b.get('birth_date') or None
    bank_code      = (b.get('bank_code') or '').strip()
    bank_name      = (b.get('bank_name') or '').strip()
    bank_branch    = (b.get('bank_branch') or '').strip()
    bank_account   = (b.get('bank_account') or '').strip()
    account_holder = (b.get('account_holder') or '').strip()
    try:
        with get_db() as conn:
            row = conn.execute("""
                INSERT INTO punch_staff
                  (name, username, password_hash, password_plain, role, employee_code,
                   department, hire_date, birth_date,
                   bank_code, bank_name, bank_branch, bank_account, account_holder)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (name, username, _hash_pw(password), password, b.get('role', '').strip(),
                  employee_code, department, hire_date, birth_date,
                  bank_code, bank_name, bank_branch, bank_account, account_holder)).fetchone()
        return jsonify(punch_staff_row(row)), 201
    except psycopg.errors.UniqueViolation:
        return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
        return jsonify({'error': f'新增失敗：{str(e)}'}), 500


@bp.route('/api/punch/staff/<int:sid>', methods=['PUT'])
@login_required
def api_punch_staff_update(sid):
    b              = request.get_json(force=True)
    name           = b.get('name', '').strip()
    username       = b.get('username', '').strip()
    password       = b.get('password', '').strip()
    role           = b.get('role', '').strip()
    active         = bool(b.get('active', True))
    employee_code  = (b.get('employee_code') or '').strip() or None
    department     = (b.get('department') or '').strip()
    hire_date      = b.get('hire_date') or None
    birth_date     = b.get('birth_date') or None
    bank_code      = (b.get('bank_code') or '').strip()
    bank_name      = (b.get('bank_name') or '').strip()
    bank_branch    = (b.get('bank_branch') or '').strip()
    bank_account   = (b.get('bank_account') or '').strip()
    account_holder = (b.get('account_holder') or '').strip()
    if not name or not username:
        return jsonify({'error': '姓名和帳號為必填'}), 400
    if password and len(password) < 4:
        return jsonify({'error': '密碼至少 4 個字元'}), 400
    try:
        with get_db() as conn:
            conn.execute("SELECT id FROM punch_staff WHERE id=%s FOR UPDATE", (sid,))
            if password:
                row = conn.execute("""
                    UPDATE punch_staff
                    SET name=%s,username=%s,password_hash=%s,password_plain=%s,role=%s,active=%s,employee_code=%s,
                        department=%s,hire_date=%s,birth_date=%s,
                        bank_code=%s,bank_name=%s,bank_branch=%s,bank_account=%s,account_holder=%s
                    WHERE id=%s RETURNING *
                """, (name, username, _hash_pw(password), password, role, active, employee_code,
                      department, hire_date, birth_date,
                      bank_code, bank_name, bank_branch, bank_account, account_holder, sid)).fetchone()
            else:
                row = conn.execute("""
                    UPDATE punch_staff
                    SET name=%s,username=%s,role=%s,active=%s,employee_code=%s,
                        department=%s,hire_date=%s,birth_date=%s,
                        bank_code=%s,bank_name=%s,bank_branch=%s,bank_account=%s,account_holder=%s
                    WHERE id=%s RETURNING *
                """, (name, username, role, active, employee_code,
                      department, hire_date, birth_date,
                      bank_code, bank_name, bank_branch, bank_account, account_holder, sid)).fetchone()
        return jsonify(punch_staff_row(row)) if row else ('', 404)
    except psycopg.errors.UniqueViolation:
        return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
        return jsonify({'error': f'更新失敗：{str(e)}'}), 500


@bp.route('/api/punch/staff/<int:sid>', methods=['DELETE'])
@login_required
def api_punch_staff_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_staff WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})


@bp.route('/api/punch/staff/<int:sid>/terminate', methods=['POST'])
@login_required
def api_staff_terminate(sid):
    b                = request.get_json(force=True)
    termination_date = b.get('termination_date', '')
    reason           = b.get('reason', '').strip()
    last_month       = b.get('last_salary_month', '')
    note             = b.get('note', '').strip()
    if not termination_date:
        return jsonify({'error': '請填寫離職日期'}), 400
    with get_db() as conn:
        try:
            conn.execute("ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS termination_date DATE")
            conn.execute("ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS termination_reason TEXT DEFAULT ''")
            conn.execute("ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS termination_note TEXT DEFAULT ''")
        except Exception:
            pass
        row = conn.execute("""
            UPDATE punch_staff SET active=FALSE,
              termination_date=%s, termination_reason=%s, termination_note=%s,
              salary_notes=COALESCE(salary_notes,'')||%s
            WHERE id=%s RETURNING *
        """, (termination_date, reason, note, f'\n【離職】{termination_date} {reason}', sid)).fetchone()
        if not row:
            return ('', 404)
    return jsonify({'ok': True, 'staff_id': sid, 'name': row['name'],
                    'termination_date': termination_date, 'last_salary_month': last_month})


@bp.route('/api/punch/staff/<int:sid>/reinstate', methods=['POST'])
@login_required
def api_staff_reinstate(sid):
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_staff SET active=TRUE,
              termination_date=NULL, termination_reason='', termination_note=''
            WHERE id=%s RETURNING *
        """, (sid,)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)


@bp.route('/api/punch/staff/terminated', methods=['GET'])
@login_required
def api_staff_terminated_list():
    with get_db() as conn:
        try:
            rows = conn.execute("""
                SELECT id, name, employee_code, department, role,
                       hire_date, termination_date, termination_reason
                FROM punch_staff WHERE active=FALSE
                ORDER BY termination_date DESC NULLS LAST, name
            """).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT id, name, employee_code, department, role, hire_date FROM punch_staff WHERE active=FALSE"
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in ('hire_date', 'termination_date'):
            if d.get(f): d[f] = str(d[f])
        result.append(d)
    return jsonify(result)


# ─── Admin: Punch Records ─────────────────────────────────────────────────────

@bp.route('/api/punch/records', methods=['GET'])
@login_required
def api_punch_records():
    staff_id  = request.args.get('staff_id')
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    month     = request.args.get('month')

    conds, params = ["TRUE"], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if month:
        conds.append("TO_CHAR(pr.punched_at,'YYYY-MM')=%s"); params.append(month)
    elif date_from:
        conds.append("(pr.punched_at AT TIME ZONE 'Asia/Taipei')::date>=%s"); params.append(date_from)
        if date_to:
            conds.append("(pr.punched_at AT TIME ZONE 'Asia/Taipei')::date<=%s"); params.append(date_to)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.punched_at DESC LIMIT 500
        """, params).fetchall()

        date_set = set()
        staff_date_pairs = set()
        for r in rows:
            pa = r['punched_at']
            if pa:
                d = pa.astimezone(TW_TZ).date()
                date_set.add(d)
                staff_date_pairs.add((r['staff_id'], d))

        holiday_map = {}
        if date_set:
            ph_rows = conn.execute(
                "SELECT date, name FROM public_holidays WHERE date = ANY(%s)",
                (list(date_set),)
            ).fetchall()
            for ph in ph_rows:
                holiday_map[ph['date']] = ph['name']

        leave_map = {}
        if staff_date_pairs:
            mn, mx = min(date_set), max(date_set)
            lv_rows = conn.execute("""
                SELECT lr.staff_id, lr.start_date, lr.end_date,
                       lr.start_half, lr.end_half,
                       lt.name as leave_name, lt.pay_rate
                FROM leave_requests lr
                JOIN leave_types lt ON lt.id=lr.leave_type_id
                WHERE lr.status='approved'
                  AND lr.start_date<=%s AND lr.end_date>=%s
            """, (mx, mn)).fetchall()
            for lv in lv_rows:
                cur = lv['start_date']
                while cur <= lv['end_date']:
                    if (lv['staff_id'], cur) in staff_date_pairs:
                        is_half = (cur == lv['start_date'] and bool(lv['start_half'])) or \
                                  (cur == lv['end_date']   and bool(lv['end_half']))
                        key = (lv['staff_id'], cur)
                        leave_map.setdefault(key, []).append({
                            'leave_name': lv['leave_name'] + ('（半天）' if is_half else ''),
                            'pay_label':  lv['pay_rate']
                        })
                    cur += _td(days=1)

    result = []
    for r in rows:
        d = punch_record_row(r)
        pa = r['punched_at']
        if pa:
            punch_date = pa.astimezone(TW_TZ).date()
            if punch_date in holiday_map:
                d['day_type']    = '國定假日'
                d['holiday_name'] = holiday_map[punch_date]
            elif punch_date.weekday() == 6:
                d['day_type'] = '例假日'
            elif punch_date.weekday() == 5:
                d['day_type'] = '休息日'
            else:
                d['day_type'] = ''
            d['leaves'] = leave_map.get((r['staff_id'], punch_date), [])
        result.append(d)
    return jsonify(result)


@bp.route('/api/punch/records', methods=['POST'])
@require_module('punch')
def api_punch_record_manual():
    b          = request.get_json(force=True)
    staff_id   = b.get('staff_id')
    punch_type = b.get('punch_type')
    punched_at = b.get('punched_at')
    note       = b.get('note', '').strip()
    manual_by  = b.get('manual_by', '').strip()
    if not all([staff_id, punch_type, punched_at]):
        return jsonify({'error': '缺少必要欄位'}), 400
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    punched_at_parsed = _parse_tw_datetime(punched_at)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, punched_at, note, is_manual, manual_by)
            VALUES (%s,%s,%s,%s,TRUE,%s) RETURNING *
        """, (staff_id, punch_type, punched_at_parsed, note, manual_by)).fetchone()
        staff = conn.execute("SELECT name FROM punch_staff WHERE id=%s", (staff_id,)).fetchone()
    d = punch_record_row(row)
    if staff: d['staff_name'] = staff['name']
    return jsonify(d), 201


@bp.route('/api/punch/records/<int:rid>', methods=['PUT'])
@require_module('punch')
def api_punch_record_update(rid):
    b = request.get_json(force=True)
    punch_type = b.get('punch_type')
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    punched_at_parsed = _parse_tw_datetime(b.get('punched_at'))
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_records
            SET punch_type=%s, punched_at=%s, note=%s, is_manual=TRUE, manual_by=%s
            WHERE id=%s RETURNING *
        """, (punch_type, punched_at_parsed,
              b.get('note', ''), b.get('manual_by', ''), rid)).fetchone()
    return jsonify(punch_record_row(row)) if row else ('', 404)


@bp.route('/api/punch/records/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


# ─── Admin: Punch Summary ─────────────────────────────────────────────────────

@bp.route('/api/punch/summary', methods=['GET'])
@login_required
def api_punch_summary():
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.staff_id, ps.name as staff_name,
                   pr.punch_type, pr.punched_at, pr.is_manual
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE pr.punched_at AT TIME ZONE 'Asia/Taipei' >=
                  date_trunc('month', TO_DATE(%s,'YYYY-MM')) - INTERVAL '1 day'
              AND pr.punched_at AT TIME ZONE 'Asia/Taipei' <
                  date_trunc('month', TO_DATE(%s,'YYYY-MM')) + INTERVAL '1 month 2 days'
            ORDER BY pr.staff_id, pr.punched_at ASC
        """, (month, month)).fetchall()

        hol_rows = conn.execute(
            "SELECT date, name FROM public_holidays WHERE TO_CHAR(date,'YYYY-MM')=%s",
            (month,)
        ).fetchall()
        holiday_map = {str(r['date']): r['name'] for r in hol_rows}

        y_ps, m_ps = int(month[:4]), int(month[5:])
        mf_ps = _date(y_ps, m_ps, 1)
        ml_ps = _date(y_ps, m_ps, _cal.monthrange(y_ps, m_ps)[1])
        lv_rows = conn.execute("""
            SELECT lr.staff_id, lr.start_date, lr.end_date,
                   lr.start_half, lr.end_half,
                   lt.name as leave_name, lt.pay_rate
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.status='approved'
              AND lr.start_date<=%s AND lr.end_date>=%s
        """, (ml_ps, mf_ps)).fetchall()

        shift_map_ps = _build_shift_time_map(conn, month)

    PAY_LBL_PS = {1.0: '全薪', 0.5: '半薪', 0.0: '無薪'}
    leave_date_map = {}
    for _lr in lv_rows:
        _sd = _lr['start_date'] if isinstance(_lr['start_date'], _date) else _date.fromisoformat(str(_lr['start_date']))
        _ed = _lr['end_date']   if isinstance(_lr['end_date'],   _date) else _date.fromisoformat(str(_lr['end_date']))
        cur = max(_sd, mf_ps)
        while cur <= min(_ed, ml_ps):
            key = (_lr['staff_id'], cur.isoformat())
            leave_date_map.setdefault(key, []).append({
                'leave_name': _lr['leave_name'] + ('（半天）' if
                    (cur == _sd and bool(_lr['start_half'])) or (cur == _ed and bool(_lr['end_half'])) else ''),
                'pay_label': PAY_LBL_PS.get(float(_lr['pay_rate']), ''),
                'pay_rate':  float(_lr['pay_rate']),
            })
            cur += _td(days=1)

    staff_names = {r['staff_id']: r['staff_name'] for r in rows}
    day_map = _shift_aware_day_map(rows, TW_TZ)

    result = []
    for (sid, ds), bucket in sorted(day_map.items(),
                                     key=lambda kv: (kv[0][1], staff_names.get(kv[0][0], '')),
                                     reverse=True):
        if not ds.startswith(month):
            continue
        ins  = bucket['ins']
        outs = bucket['outs']
        clock_in  = min(ins).isoformat()  if ins  else None
        clock_out = max(outs).isoformat() if outs else None
        punch_count = len(ins) + len(outs) + len(bucket['break_outs']) + len(bucket['break_ins'])
        duration_min = None
        if ins and outs:
            ws, we = _clamp_to_shift(min(ins), max(outs), shift_map_ps, sid, ds)
            if ws is not None:
                gross_min = max(0, int((we - ws).total_seconds() / 60))
                brk = 0.0
                for bo in bucket['break_outs']:
                    matched = [bi for bi in bucket['break_ins'] if bi > bo]
                    if matched:
                        brk += (min(matched) - bo).total_seconds() / 60
                if gross_min >= 540: brk = max(brk, 60)
                elif gross_min >= 240: brk = max(brk, 30)
                duration_min = max(0, int(gross_min - brk))
        result.append({
            'staff_id':     sid,
            'staff_name':   staff_names.get(sid, ''),
            'work_date':    ds,
            'clock_in':     clock_in,
            'clock_out':    clock_out,
            'punch_count':  punch_count,
            'has_manual':   bucket['has_manual'],
            'duration_min': duration_min,
            'holiday_name': holiday_map.get(ds, ''),
            'leaves':       leave_date_map.get((sid, ds), []),
        })
    return jsonify(result)


@bp.route('/api/attendance/monthly-stats', methods=['GET'])
@login_required
def api_attendance_monthly_stats():
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.staff_id, ps.name as staff_name, ps.department, ps.role,
                   pr.punch_type, pr.punched_at, pr.is_manual
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id=pr.staff_id AND ps.active=TRUE
            WHERE pr.punched_at AT TIME ZONE 'Asia/Taipei' >=
                  date_trunc('month', TO_DATE(%s,'YYYY-MM')) - INTERVAL '1 day'
              AND pr.punched_at AT TIME ZONE 'Asia/Taipei' <
                  date_trunc('month', TO_DATE(%s,'YYYY-MM')) + INTERVAL '1 month 2 days'
            ORDER BY pr.staff_id, pr.punched_at ASC
        """, (month, month)).fetchall()

        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time
            FROM shift_assignments sa JOIN shift_types st ON st.id=sa.shift_type_id
            WHERE TO_CHAR(sa.shift_date,'YYYY-MM')=%s
        """, (month,)).fetchall()
        shift_map      = {(r['staff_id'], str(r['shift_date'])): r for r in shift_rows}
        shift_time_map = _build_shift_time_map(conn, month)

    staff_info = {}
    for r in rows:
        if r['staff_id'] not in staff_info:
            staff_info[r['staff_id']] = {
                'staff_name': r['staff_name'],
                'department': r['department'] or '',
                'role':       r['role']       or '',
            }

    day_map = _shift_aware_day_map(rows, TW_TZ)
    stats   = defaultdict(lambda: {
        'staff_id': None, 'staff_name': '', 'department': '', 'role': '',
        'days_worked': 0, 'total_minutes': 0,
        'late_count': 0, 'early_count': 0, 'missing_in_count': 0, 'missing_out_count': 0,
        'anomaly_dates': [],
    })

    for (sid, ds), bucket in day_map.items():
        if not ds.startswith(month):
            continue
        s    = stats[sid]
        info = staff_info.get(sid, {})
        s['staff_id']   = sid
        s['staff_name'] = info.get('staff_name', '')
        s['department'] = info.get('department', '')
        s['role']       = info.get('role', '')
        has_in  = bool(bucket['ins'])
        has_out = bool(bucket['outs'])
        if has_in or has_out:
            s['days_worked'] += 1
        if has_in and has_out:
            ws, we = _clamp_to_shift(min(bucket['ins']), max(bucket['outs']), shift_time_map, sid, ds)
            if ws is not None:
                diff = (we - ws).total_seconds() / 60
                if diff > 0:
                    brk = 0.0
                    for bo in bucket['break_outs']:
                        matched = [bi for bi in bucket['break_ins'] if bi > bo]
                        if matched:
                            brk += (min(matched) - bo).total_seconds() / 60
                    if diff >= 540:   brk = max(brk, 60)
                    elif diff >= 240: brk = max(brk, 30)
                    s['total_minutes'] += int(diff - brk)
        if has_in and not has_out:
            s['missing_out_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_out', 'label': '缺下班卡'})
        if not has_in and has_out:
            s['missing_in_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_in', 'label': '缺上班卡'})
        if has_in and has_out and len(bucket['ins']) != len(bucket['outs']):
            s['anomaly_dates'].append({'date': ds, 'type': 'unmatched_punch',
                                       'label': f'配對異常（上班 {len(bucket["ins"])} 次 / 下班 {len(bucket["outs"])} 次）'})
        if has_in:
            clock_in = min(bucket['ins'])
            shift = shift_map.get((sid, ds))
            if shift and shift['start_time']:
                try:
                    sh, sm = map(int, str(shift['start_time'])[:5].split(':'))
                    late_mins = (clock_in.hour * 60 + clock_in.minute) - (sh * 60 + sm)
                    if late_mins > 10:
                        s['late_count'] += 1
                        s['anomaly_dates'].append({'date': ds, 'type': 'late', 'label': f'遲到 {late_mins} 分鐘'})
                except Exception:
                    pass
        if has_out:
            clock_out = max(bucket['outs'])
            shift = shift_map.get((sid, ds))
            if shift and shift['end_time']:
                try:
                    eh, em = map(int, str(shift['end_time'])[:5].split(':'))
                    early_mins = (eh * 60 + em) - (clock_out.hour * 60 + clock_out.minute)
                    if early_mins > 15:
                        s['early_count'] += 1
                        s['anomaly_dates'].append({'date': ds, 'type': 'early', 'label': f'早退 {early_mins} 分鐘'})
                except Exception:
                    pass

    result = []
    for s in sorted(stats.values(), key=lambda x: (x['department'], x['staff_name'])):
        s['total_hours']   = round(s['total_minutes'] / 60, 1)
        s['avg_hours_day'] = round(s['total_minutes'] / s['days_worked'] / 60, 1) if s['days_worked'] else 0
        s['total_hm']      = f"{s['total_minutes']//60}h {s['total_minutes']%60:02d}m"
        result.append(s)
    return jsonify({'month': month, 'stats': result})


# ─── Punch Requests (補打卡申請) ──────────────────────────────────────────────

@bp.route('/api/punch/request', methods=['POST'])
def api_punch_req_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b            = request.get_json(force=True)
    punch_type   = b.get('punch_type')
    requested_at = b.get('requested_at')
    reason       = b.get('reason', '').strip()
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    if not requested_at:
        return jsonify({'error': '請選擇補打時間'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO punch_requests (staff_id, punch_type, requested_at, reason)
            VALUES (%s,%s,%s,%s) RETURNING *
        """, (sid, punch_type, requested_at, reason)).fetchone()
    return jsonify(punch_req_row(row)), 201


@bp.route('/api/punch/request/my', methods=['GET'])
def api_punch_req_my():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM punch_requests WHERE staff_id=%s ORDER BY requested_at DESC LIMIT 20",
            (sid,)
        ).fetchall()
    return jsonify([punch_req_row(r) for r in rows])


@bp.route('/api/punch/requests', methods=['GET'])
@login_required
def api_punch_reqs_list():
    status = request.args.get('status', '')
    conds, params = ['TRUE'], []
    if status: conds.append('pr.status=%s'); params.append(status)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_requests pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.created_at DESC LIMIT 200
        """, params).fetchall()
    return jsonify([punch_req_row(r) for r in rows])


@bp.route('/api/punch/requests/<int:rid>', methods=['PUT'])
@login_required
def api_punch_req_review(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'invalid action'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_requests
            SET status=%s, reviewed_by=%s, review_note=%s, reviewed_at=NOW()
            WHERE id=%s AND status='pending'
            RETURNING *, (SELECT name FROM punch_staff WHERE id=staff_id) as staff_name
        """, (new_status, reviewed_by, review_note, rid)).fetchone()
        if not row: return ('', 404)
        if action == 'approve':
            existing = conn.execute("""
                SELECT id FROM punch_records
                WHERE staff_id=%s AND punch_type=%s AND punched_at=%s
            """, (row['staff_id'], row['punch_type'], row['requested_at'])).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO punch_records
                      (staff_id, punch_type, punched_at, note, is_manual, manual_by)
                    VALUES (%s,%s,%s,%s,TRUE,%s)
                """, (row['staff_id'], row['punch_type'], row['requested_at'],
                      f'補打卡申請 #{rid}：{row["reason"]}', reviewed_by))
    LABEL  = {'in': '上班打卡', 'out': '下班打卡', 'break_out': '休息開始', 'break_in': '休息結束'}
    dt_str = row['requested_at'].isoformat()[:16].replace('T', ' ')
    extra  = f"{LABEL.get(row['punch_type'],'')} {dt_str}"
    if review_note: extra += f"\n審核意見：{review_note}"
    _notify_review_result(row['staff_id'], '補打卡申請', action, extra)
    return jsonify(punch_req_row(row))


@bp.route('/api/punch/requests/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_req_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


@bp.route('/api/punch/requests/batch', methods=['POST'])
@login_required
def api_punch_req_batch():
    b      = request.get_json(force=True)
    ids    = [int(i) for i in b.get('ids', [])]
    action = b.get('action')
    by     = b.get('reviewed_by', '管理員')
    note   = b.get('review_note', '')
    if not ids or action not in ('approve', 'reject'):
        return jsonify({'error': '參數錯誤'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    done = 0
    with get_db() as conn:
        for rid in ids:
            row = conn.execute("""
                UPDATE punch_requests SET status=%s, reviewed_by=%s,
                  review_note=%s, reviewed_at=NOW()
                WHERE id=%s AND status='pending' RETURNING *
            """, (new_status, by, note, rid)).fetchone()
            if row:
                if action == 'approve':
                    existing = conn.execute("""
                        SELECT id FROM punch_records
                        WHERE staff_id=%s AND punch_type=%s AND punched_at=%s
                    """, (row['staff_id'], row['punch_type'], row['requested_at'])).fetchone()
                    if not existing:
                        conn.execute("""
                            INSERT INTO punch_records
                              (staff_id, punch_type, punched_at, note, is_manual, manual_by)
                            VALUES (%s,%s,%s,%s,TRUE,%s)
                        """, (row['staff_id'], row['punch_type'], row['requested_at'],
                              f'補打卡申請#{rid}', by))
                _notify_review_result(row['staff_id'], '補打卡申請', action,
                                      f'批次審核意見：{note}' if note else '')
                done += 1
    return jsonify({'ok': True, 'done': done})
