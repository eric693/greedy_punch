import json as _json

from flask import Blueprint, request, jsonify

from auth import login_required
from database import get_db

bp = Blueprint('stores', __name__)


# ── Stores CRUD ───────────────────────────────────────────────────────────────

@bp.route('/api/stores', methods=['GET'])
@login_required
def api_stores_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM stores ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/stores', methods=['POST'])
@login_required
def api_stores_create():
    b    = request.get_json(force=True)
    name = (b.get('name') or '').strip()
    code = (b.get('code') or '').strip() or None
    if not name: return jsonify({'error': '店名為必填'}), 400
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO stores (name, code, address) VALUES (%s,%s,%s) RETURNING *",
            (name, code, (b.get('address') or '').strip())
        ).fetchone()
    return jsonify(dict(row)), 201


@bp.route('/api/stores/<int:sid>', methods=['PUT'])
@login_required
def api_stores_update(sid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE stores SET name=%s, code=%s, address=%s, active=%s WHERE id=%s RETURNING *
        """, ((b.get('name') or '').strip(), (b.get('code') or None),
              (b.get('address') or '').strip(), bool(b.get('active', True)), sid)).fetchone()
    return jsonify(dict(row)) if row else ('', 404)


@bp.route('/api/stores/<int:sid>', methods=['DELETE'])
@login_required
def api_stores_delete(sid):
    with get_db() as conn:
        conn.execute("UPDATE punch_staff     SET store_id=NULL WHERE store_id=%s", (sid,))
        conn.execute("UPDATE punch_locations SET store_id=NULL WHERE store_id=%s", (sid,))
        conn.execute("DELETE FROM stores WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})


@bp.route('/api/stores/<int:sid>/staff', methods=['GET'])
@login_required
def api_store_staff(sid):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, role, active FROM punch_staff WHERE store_id=%s ORDER BY name", (sid,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/staff/<int:sid>/store', methods=['PUT'])
@login_required
def api_staff_assign_store(sid):
    b        = request.get_json(force=True)
    store_id = b.get('store_id')
    with get_db() as conn:
        conn.execute("UPDATE punch_staff SET store_id=%s WHERE id=%s", (store_id, sid))
    return jsonify({'ok': True})


# ── Staffing Requirements ─────────────────────────────────────────────────────

@bp.route('/api/shifts/staffing-requirements', methods=['GET'])
@login_required
def api_staffing_req_get():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT r.id, r.shift_type_id, r.day_of_week, r.required_count,
                   st.name as shift_name, st.color as shift_color
            FROM shift_staffing_requirements r
            JOIN shift_types st ON st.id=r.shift_type_id
            ORDER BY st.sort_order, r.day_of_week
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/shifts/staffing-requirements', methods=['PUT'])
@login_required
def api_staffing_req_put():
    items = request.get_json(force=True)
    if not isinstance(items, list):
        return jsonify({'error': '格式錯誤'}), 400
    count = 0
    with get_db() as conn:
        for it in items:
            stid = int(it.get('shift_type_id', 0))
            dow  = int(it.get('day_of_week', 0))
            req  = max(0, int(it.get('required_count', 1)))
            if req == 0:
                conn.execute(
                    "DELETE FROM shift_staffing_requirements WHERE shift_type_id=%s AND day_of_week=%s",
                    (stid, dow))
            else:
                conn.execute("""
                    INSERT INTO shift_staffing_requirements (shift_type_id, day_of_week, required_count, updated_at)
                    VALUES (%s,%s,%s,NOW())
                    ON CONFLICT (shift_type_id, day_of_week)
                    DO UPDATE SET required_count=EXCLUDED.required_count, updated_at=NOW()
                """, (stid, dow, req))
            count += 1
    return jsonify({'ok': True, 'upserted': count})


# ── Auto Schedule Generation ──────────────────────────────────────────────────

@bp.route('/api/schedule/auto-generate', methods=['POST'])
@login_required
def api_auto_generate_schedule():
    from datetime import date as _d, timedelta as _td
    import calendar as _cal

    b         = request.get_json(force=True)
    month     = (b.get('month') or '').strip()
    overwrite = bool(b.get('overwrite', False))
    if not month:
        month = _d.today().strftime('%Y-%m')
    try:
        y, mo = int(month[:4]), int(month[5:7])
    except Exception:
        return jsonify({'error': '月份格式錯誤'}), 400

    days_in   = _cal.monthrange(y, mo)[1]
    all_dates = [_d(y, mo, day) for day in range(1, days_in + 1)]

    with get_db() as conn:
        shift_types  = conn.execute(
            "SELECT * FROM shift_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
        requirements = conn.execute("""
            SELECT shift_type_id, day_of_week, required_count
            FROM shift_staffing_requirements
        """).fetchall()
        staff_list   = conn.execute(
            "SELECT id, name FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        leave_rows   = conn.execute("""
            SELECT staff_id, start_date, end_date FROM leave_requests
            WHERE status='approved'
              AND start_date <= %s AND end_date >= %s
        """, (f'{y}-{mo:02d}-{days_in:02d}', f'{y}-{mo:02d}-01')).fetchall()
        sched_rows   = conn.execute("""
            SELECT staff_id, dates FROM schedule_requests
            WHERE status='approved' AND month=%s
        """, (month,)).fetchall()
        existing     = conn.execute("""
            SELECT staff_id, shift_date FROM shift_assignments
            WHERE TO_CHAR(shift_date,'YYYY-MM')=%s
        """, (month,)).fetchall()

    off_days = set()
    for lr in leave_rows:
        cur = _d.fromisoformat(str(lr['start_date']))
        end = _d.fromisoformat(str(lr['end_date']))
        while cur <= end:
            off_days.add((lr['staff_id'], str(cur))); cur += _td(days=1)
    for sr in sched_rows:
        rdates = sr['dates']
        if isinstance(rdates, str):
            try: rdates = _json.loads(rdates)
            except: rdates = []
        for ds in (rdates or []):
            off_days.add((sr['staff_id'], ds))

    existing_set = {(r['staff_id'], str(r['shift_date'])) for r in existing}
    req_map      = {(r['shift_type_id'], r['day_of_week']): r['required_count'] for r in requirements}

    assigned_days  = {s['id']: [] for s in staff_list}
    assignments    = []
    conflicts      = []
    staff_ids      = [s['id'] for s in staff_list]
    staff_name_map = {s['id']: s['name'] for s in staff_list}

    for date in all_dates:
        dow = date.weekday()
        ds  = str(date)
        for st in shift_types:
            stid   = st['id']
            needed = req_map.get((stid, dow), 0)
            if needed <= 0: continue

            available = [sid for sid in staff_ids if (sid, ds) not in off_days]
            already_today = {a['staff_id'] for a in assignments if a['shift_date'] == ds}
            available = [sid for sid in available if sid not in already_today]

            def consecutive_days(sid, d):
                days = sorted(assigned_days[sid])
                streak = 0; check = d
                while check in days:
                    streak += 1
                    check = str(_d.fromisoformat(check) - _td(days=1))
                return streak

            available_ok = [sid for sid in available if consecutive_days(sid, ds) < 6]
            available_ok.sort(key=lambda sid: len(assigned_days[sid]))

            assigned_count = 0
            for sid in available_ok:
                if assigned_count >= needed: break
                if not overwrite and (sid, ds) in existing_set:
                    assigned_count += 1; continue
                assignments.append({
                    'staff_id':      sid,
                    'staff_name':    staff_name_map[sid],
                    'shift_type_id': stid,
                    'shift_name':    st['name'],
                    'shift_date':    ds,
                })
                assigned_days[sid].append(ds)
                assigned_count += 1

            if assigned_count < needed:
                conflicts.append({
                    'type':   'understaffed',
                    'date':   ds,
                    'shift':  st['name'],
                    'detail': f'{ds} {st["name"]} 需要 {needed} 人，僅能排 {assigned_count} 人',
                })

    inserted = 0
    if assignments:
        with get_db() as conn:
            for a in assignments:
                try:
                    if overwrite:
                        conn.execute("""
                            INSERT INTO shift_assignments (staff_id, shift_type_id, shift_date)
                            VALUES (%s,%s,%s)
                            ON CONFLICT (staff_id, shift_date) DO UPDATE
                            SET shift_type_id=EXCLUDED.shift_type_id
                        """, (a['staff_id'], a['shift_type_id'], a['shift_date']))
                    else:
                        conn.execute("""
                            INSERT INTO shift_assignments (staff_id, shift_type_id, shift_date)
                            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                        """, (a['staff_id'], a['shift_type_id'], a['shift_date']))
                    inserted += 1
                except Exception:
                    pass

    return jsonify({
        'ok': True, 'month': month,
        'assignments': assignments, 'conflicts': conflicts,
        'summary': {'assigned': inserted, 'conflict_count': len(conflicts)},
    })
