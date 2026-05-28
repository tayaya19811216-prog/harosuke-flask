import os
import re
import html as html_module
import json
import time
import uuid
import datetime
import requests
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

BASE_URL = "https://helloproject.com"
SCHEDULE_URL = f"{BASE_URL}/schedule/"
JSON_BASE = f"{BASE_URL}/json"
CACHE_TTL = 3600  # 1時間

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_page_cache = {"data": None, "ts": 0}
_schedule_cache = {}


def decode_astro_value(v):
    if isinstance(v, list) and len(v) == 2:
        tag, val = v
        if tag == 0:
            return val
        if tag == 1:
            return [decode_astro_value(x) for x in val]
    return v


def decode_astro_obj(obj):
    if isinstance(obj, list) and len(obj) == 2:
        tag, val = obj
        if tag == 0 and isinstance(val, dict):
            return {k: decode_astro_value(v) for k, v in val.items()}
    return obj


def fetch_schedule_page_data():
    now = time.time()
    if _page_cache["data"] and now - _page_cache["ts"] < CACHE_TTL:
        return _page_cache["data"]

    resp = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    content = resp.text

    m = re.search(
        r'<astro-island[^>]+component-url="/_astro/Schedule\.[^"]+\.js"[^>]+props="(\{[^>]+?})"',
        content,
    )
    if not m:
        m = re.search(r'props="(\{[^"]{200,})"', content)
    if not m:
        return None

    props_raw = html_module.unescape(m.group(1))
    props = json.loads(props_raw)

    version_dir = decode_astro_value(props.get("versionDir", [0, ""]))

    raw_profiles = props.get("allProfiles", [0, {}])
    profiles_data = decode_astro_value(raw_profiles)
    profiles = {}
    if isinstance(profiles_data, dict):
        for artist_id, val in profiles_data.items():
            decoded = decode_astro_obj(val)
            if isinstance(decoded, dict):
                profiles[str(artist_id)] = {
                    "nameJa": decoded.get("nameJa", ""),
                    "nameEn": decoded.get("nameEn", ""),
                    "slug": decoded.get("slug", ""),
                    "type": decoded.get("type", ""),
                }

    years_raw = props.get("years", [1, []])
    years_list = decode_astro_value(years_raw)
    years = [str(y) for y in years_list] if isinstance(years_list, list) else ["2026"]

    result = {"version_dir": version_dir, "profiles": profiles, "years": years}
    _page_cache["data"] = result
    _page_cache["ts"] = now
    return result


def fetch_schedules(version_dir, year):
    cache_key = f"{version_dir}:{year}"
    now = time.time()
    if cache_key in _schedule_cache:
        cached_ts, cached_items = _schedule_cache[cache_key]
        if now - cached_ts < CACHE_TTL:
            return cached_items

    url = f"{JSON_BASE}/{version_dir}/{year}_schedules.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    _schedule_cache[cache_key] = (now, items)
    return items


def get_cache_status():
    now = time.time()
    page_age = now - _page_cache["ts"] if _page_cache["ts"] else None
    page_remaining = max(0, CACHE_TTL - page_age) if page_age is not None else 0
    schedule_info = {}
    for k, (ts, items) in _schedule_cache.items():
        age = now - ts
        schedule_info[k] = {
            "remaining": max(0, int(CACHE_TTL - age)),
            "count": len(items),
        }
    return {
        "page_remaining_seconds": int(page_remaining),
        "schedules": schedule_info,
    }


@app.route("/")
def index():
    error = None
    profiles = {}
    years = []
    selected_year = request.args.get("year", "")

    try:
        page_data = fetch_schedule_page_data()
        if not page_data or not page_data.get("version_dir"):
            raise ValueError("versionDir の取得に失敗しました")
        profiles = page_data["profiles"]
        years = page_data["years"]
        if not selected_year and years:
            selected_year = years[0]
    except Exception as e:
        error = f"データ取得に失敗しました: {e}"

    groups = {k: v for k, v in profiles.items() if v.get("type") == "group"}
    categories = [
        "CONCERT", "EVENT", "STAGE", "STREAM",
        "TV", "RADIO", "MAGAZINE", "WEB",
        "RELEASE", "BIRTHDAY", "ANNIVERSARY", "OTHER",
    ]

    return render_template(
        "index.html",
        profiles=profiles,
        groups=groups,
        years=years,
        selected_year=selected_year,
        categories=categories,
        error=error,
    )


@app.route("/api/events")
def api_events():
    year = request.args.get("year", "")
    group = request.args.get("group", "")
    member = request.args.get("member", "")
    category = request.args.get("category", "")

    try:
        page_data = fetch_schedule_page_data()
        if not page_data or not page_data.get("version_dir"):
            return jsonify({"error": "versionDir の取得に失敗しました"}), 500

        version_dir = page_data["version_dir"]
        years = page_data["years"]
        if not year and years:
            year = years[0]

        items = fetch_schedules(version_dir, year)

        if group:
            items = [s for s in items if group in [str(a) for a in s.get("artistsSearch", [])]]
        if member:
            items = [s for s in items if member in [str(m) for m in s.get("membersSearch", [])]]
        if category:
            items = [s for s in items if s.get("category", "").upper() == category.upper()]

        events = []
        for item in items:
            title = item.get("title", {}).get("content", "（タイトルなし）")
            date = item.get("date", "")
            cat = item.get("category", "")
            tags = item.get("tags", [])
            sched = item.get("schedule", {})
            link = item.get("link", {})
            note = (item.get("note") or {}).get("content", "")

            time_label = ""
            if sched.get("label"):
                time_label = sched["label"]
            elif sched.get("start"):
                time_label = sched["start"]
                if sched.get("end") and sched["end"] != sched["start"]:
                    time_label += f"〜{sched['end']}"

            events.append({
                "id": f"{date}-{len(events)}",
                "title": title,
                "start": date,
                "extendedProps": {
                    "category": cat,
                    "tags": tags,
                    "timeLabel": time_label,
                    "note": note[:200] if note else "",
                    "link": link.get("link", "") if link else "",
                    "targetBlank": link.get("targetBlank", False) if link else False,
                    "isFc": item.get("isFc", False),
                },
            })

        return jsonify(events)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def build_member_group_map(items):
    """グループ単独イベント（artistsSearch が1グループのみ）からメンバー→グループの対応を構築する。"""
    member_groups = {}
    for item in items:
        artists = [str(a) for a in item.get("artistsSearch", [])]
        if len(artists) == 1:
            gid = artists[0]
            for mid in item.get("membersSearch", []):
                mid = str(mid)
                member_groups.setdefault(mid, set()).add(gid)
    return member_groups


@app.route("/api/members")
def api_members():
    group_id = request.args.get("group", "")
    year = request.args.get("year", "")

    try:
        page_data = fetch_schedule_page_data()
        if not page_data:
            return jsonify([])
        profiles = page_data["profiles"]
        version_dir = page_data["version_dir"]
        years = page_data["years"]
        if not year and years:
            year = years[0]

        items = fetch_schedules(version_dir, year)

        if group_id:
            # グループ単独イベントから確実なメンバー所属を判定
            member_group_map = build_member_group_map(items)
            member_ids = {
                mid for mid, grps in member_group_map.items() if group_id in grps
            }
        else:
            # グループ未選択時は全メンバーを返す
            member_ids = set()
            for item in items:
                for mid in item.get("membersSearch", []):
                    member_ids.add(str(mid))

        members = []
        for mid in member_ids:
            prof = profiles.get(mid)
            if prof and prof.get("type") == "member":
                members.append({
                    "id": mid,
                    "name": prof.get("nameJa") or prof.get("nameEn") or mid,
                    "nameEn": prof.get("nameEn") or prof.get("nameJa") or mid,
                })

        members.sort(key=lambda x: x["name"])
        return jsonify(members)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def ical_escape(text):
    if not text:
        return ""
    text = str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return text


def ical_fold(line):
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    result = []
    while len(encoded) > 75:
        chunk = encoded[:75].decode("utf-8", errors="ignore")
        result.append(chunk)
        encoded = encoded[75:]
    result.append(encoded.decode("utf-8", errors="ignore"))
    return "\r\n ".join(result)


def date_str_to_ical(date_str):
    return date_str.replace("-", "")


def next_day_ical(date_str):
    d = datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def build_ical(items, calendar_name="ハロー！プロジェクト スケジュール"):
    now_stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hello! Project Schedule//JP",
        f"X-WR-CALNAME:{ical_escape(calendar_name)}",
        "X-WR-TIMEZONE:Asia/Tokyo",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    for item in items:
        title = (item.get("title") or {}).get("content", "（タイトルなし）")
        date = item.get("date", "")
        cat = item.get("category", "")
        tags = item.get("tags", [])
        sched = item.get("schedule", {}) or {}
        link = item.get("link", {}) or {}
        note_raw = (item.get("note") or {}).get("content", "")

        time_label = sched.get("label", "")
        if not time_label and sched.get("start"):
            time_label = sched["start"]
            if sched.get("end") and sched["end"] != sched["start"]:
                time_label += f"〜{sched['end']}"

        description_parts = []
        if cat:
            description_parts.append(f"【{cat}】")
        if time_label:
            description_parts.append(f"時間: {time_label}")
        if tags:
            description_parts.append(f"出演: {', '.join(tags)}")
        if note_raw:
            description_parts.append(note_raw)
        description = "\\n".join(ical_escape(p) for p in description_parts)

        event_url = link.get("link", "") if link else ""
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"helloproject-{date}-{title}"))

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(ical_fold(f"DTSTART;VALUE=DATE:{date_str_to_ical(date)}"))
        lines.append(ical_fold(f"DTEND;VALUE=DATE:{next_day_ical(date)}"))
        lines.append(ical_fold(f"SUMMARY:{ical_escape(title)}"))
        if description:
            lines.append(ical_fold(f"DESCRIPTION:{description}"))
        if event_url:
            lines.append(ical_fold(f"URL:{event_url}"))
        if cat:
            lines.append(ical_fold(f"CATEGORIES:{ical_escape(cat)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.route("/api/export/ical")
def api_export_ical():
    year = request.args.get("year", "")
    group = request.args.get("group", "")
    member = request.args.get("member", "")
    category = request.args.get("category", "")

    try:
        page_data = fetch_schedule_page_data()
        if not page_data or not page_data.get("version_dir"):
            return Response("データ取得に失敗しました", status=500, mimetype="text/plain")

        version_dir = page_data["version_dir"]
        profiles = page_data["profiles"]
        years = page_data["years"]
        if not year and years:
            year = years[0]

        items = fetch_schedules(version_dir, year)

        if group:
            items = [s for s in items if group in [str(a) for a in s.get("artistsSearch", [])]]
        if member:
            items = [s for s in items if member in [str(m) for m in s.get("membersSearch", [])]]
        if category:
            items = [s for s in items if s.get("category", "").upper() == category.upper()]

        items = sorted(items, key=lambda s: s.get("date", ""))

        cal_name_parts = ["ハロー！プロジェクト"]
        if group and group in profiles:
            cal_name_parts.append(profiles[group].get("nameJa") or profiles[group].get("nameEn") or "")
        if member and member in profiles:
            cal_name_parts.append(profiles[member].get("nameJa") or profiles[member].get("nameEn") or "")
        if category:
            cal_name_parts.append(category)
        cal_name_parts.append(f"{year}年")
        calendar_name = " - ".join(p for p in cal_name_parts if p)

        ical_content = build_ical(items, calendar_name)

        filename = f"helloproject_{year}"
        if group:
            slug = (profiles.get(group) or {}).get("slug") or group
            filename += f"_{slug}"
        if member:
            slug = (profiles.get(member) or {}).get("slug") or member
            filename += f"_{slug}"
        if category:
            filename += f"_{category.lower()}"
        filename += ".ics"

        return Response(
            ical_content,
            mimetype="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "text/calendar; charset=utf-8",
            },
        )

    except Exception as e:
        return Response(f"エクスポートに失敗しました: {e}", status=500, mimetype="text/plain")


@app.route("/api/export/ical/single")
def api_export_ical_single():
    date = request.args.get("date", "")
    title = request.args.get("title", "イベント")
    desc = request.args.get("desc", "")
    event_url = request.args.get("url", "")
    cat = request.args.get("cat", "")

    if not date:
        return Response("dateパラメータが必要です", status=400, mimetype="text/plain")

    item = {
        "title": {"content": title},
        "date": date,
        "category": cat,
        "tags": [],
        "schedule": {},
        "link": {"link": event_url, "targetBlank": True} if event_url else None,
        "note": {"content": desc} if desc else None,
    }

    ical_content = build_ical([item], title)
    safe_title = re.sub(r"[^\w\-]", "_", title)[:40]
    filename = f"helloproject_{date}_{safe_title}.ics"

    return Response(
        ical_content,
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/calendar; charset=utf-8",
        },
    )


@app.route("/api/cache-status")
def api_cache_status():
    return jsonify(get_cache_status())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
