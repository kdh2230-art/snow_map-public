"""
기상청 예보 -> 지정 지점의 '시각별' 눈/비 여부를 지도에 색으로 표시

- 내 PC에서:  python snow_map.py  -> snow_map.html 생성 후 브라우저로 열림
              (인증키는 같은 폴더의 key.txt 에 한 줄로 저장)
- GitHub Actions에서: 환경변수 SERVICE_KEY 사용 -> site/index.html 생성 (자동 게시용)
"""
import json
import math
import os
import sys
import time
import traceback
import webbrowser
from datetime import datetime, timedelta

import folium
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
IS_CI = bool(os.environ.get("GITHUB_ACTIONS"))
HOURS_AHEAD = 72  # 앞으로 몇 시간까지 볼지 (단기예보는 최대 5일=120)

# 위도, 경도는 대략값입니다. 필요하면 수정하세요.
PLACES = {
    "산내JC": (36.1900, 127.4700),
    "추부IC": (36.1830, 127.3600),
    "금산IC": (36.1000, 127.4700),
    "무주읍": (36.0070, 127.6610),
    "덕유산IC": (35.9440, 127.6500),
    "장수JC": (35.6640, 127.5500),
    "서상IC": (35.6550, 127.7180),
}

LABEL_DIR = {"추부IC": "left", "장수JC": "left"}  # 나머지는 오른쪽

PTY_NAME = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "4": "소나기",
            "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림", "?": "자료 없음"}
PTY_COLOR = {"0": "#9ca3af", "1": "#059669", "2": "#7c3aed", "3": "#2563eb",
             "4": "#059669", "5": "#059669", "6": "#7c3aed", "7": "#2563eb", "?": "#374151"}
WEEK = "월화수목금토일"


def load_key():
    k = os.environ.get("SERVICE_KEY", "").strip()
    if k:
        return k
    p = os.path.join(HERE, "key.txt")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    return ""


SERVICE_KEY = load_key()


def now_kst():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def latlon_to_grid(lat, lon):
    """기상청 격자(nx, ny) 변환 (Lambert Conformal Conic)"""
    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT = 30.0, 60.0, 126.0, 38.0
    XO, YO = 43, 136
    D = math.pi / 180.0
    re = RE / GRID
    slat1, slat2, olon, olat = SLAT1 * D, SLAT2 * D, OLON * D, OLAT * D
    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)
    ra = math.tan(math.pi * 0.25 + lat * D * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * D - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    nx = int(math.floor(ra * math.sin(theta) + XO + 0.5))
    ny = int(math.floor(ro - ra * math.cos(theta) + YO + 0.5))
    return nx, ny


def latest_base(now):
    """단기예보: 02,05,08,11,14,17,20,23시 발표 (약 10분 뒤부터 조회 가능)"""
    hours = [2, 5, 8, 11, 14, 17, 20, 23]
    t = now - timedelta(minutes=10)
    cands = [h for h in hours if h <= t.hour]
    if cands:
        return t.strftime("%Y%m%d"), f"{cands[-1]:02d}00"
    y = t - timedelta(days=1)
    return y.strftime("%Y%m%d"), "2300"


def latest_ultra_base(now):
    """초단기예보: 매시 30분 발표 (약 45분 뒤부터 조회 가능, 6시간 이내 예보)"""
    t = now - timedelta(minutes=45)
    return t.strftime("%Y%m%d"), f"{t.hour:02d}30"


def _fetch(endpoint, nx, ny, base_date, base_time, rows):
    url = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/" + endpoint
    params = {
        "serviceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": rows,
        "dataType": "JSON", "base_date": base_date, "base_time": base_time,
        "nx": nx, "ny": ny,
    }
    last = None
    for attempt in range(3):  # 일시적 실패 대비 3회 재시도
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            items = r.json()["response"]["body"]["items"]["item"]
            acc = {}
            for i in items:
                if i["category"] in ("PTY", "TMP", "T1H"):
                    acc.setdefault((i["fcstDate"], i["fcstTime"]), {})[i["category"]] = i["fcstValue"]
            # {(날짜, 시각): (강수형태, 기온)}
            return {k: (v["PTY"], v.get("TMP", v.get("T1H"))) for k, v in acc.items() if "PTY" in v}
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def to_temp(v):
    try:
        return int(round(float(v)))
    except Exception:
        return None


def hour_label(dt):
    return f"{dt:%m/%d}({WEEK[dt.weekday()]}) {dt:%H}시"


UI = """
<style>
html,body{font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif;color:#111827}
.card{background:rgba(255,255,255,.97);border-radius:14px;box-shadow:0 4px 18px rgba(0,0,0,.2)}
#head{position:fixed;top:12px;left:12px;z-index:9999;width:330px;padding:14px 16px;box-sizing:border-box}
#head .top{display:flex;justify-content:space-between;align-items:baseline;font-size:12px;color:#6b7280}
#head .ttl{font-weight:800;font-size:14px;color:#111827}
#when{font-size:24px;font-weight:800;margin:8px 0}
#sum{border-radius:10px;padding:9px 11px;font-size:14px;font-weight:700;line-height:1.45}
#sum.snow{background:#dbeafe;color:#1e40af}
#sum.mix{background:#ede9fe;color:#5b21b6}
#sum.rain{background:#d1fae5;color:#065f46}
#sum.clear{background:#f3f4f6;color:#374151}
#tmp{font-size:12px;color:#6b7280;margin-top:6px}
#head .row{display:flex;gap:8px;align-items:center;margin-top:12px}
#head .row button{border:0;background:#111827;color:#fff;border-radius:10px;width:42px;height:38px;font-size:15px;cursor:pointer}
#rng{flex:1;accent-color:#111827;min-width:0}
#legend{font-size:11px;color:#6b7280;margin-top:10px}
#panel{position:fixed;top:12px;right:12px;z-index:9999;display:flex;flex-direction:column;
  max-height:calc(100vh - 24px);overflow:hidden}
#grab{display:none}
#scroll{overflow:auto}
#panel table{border-collapse:separate;border-spacing:0;font-size:12px;width:100%}
#panel th{position:sticky;top:0;background:#fff;z-index:2;padding:9px 2px;font-size:11px;color:#6b7280;
  font-weight:700;border-bottom:1px solid #e5e7eb}
#panel td{padding:0}
#panel td.t{padding:7px 9px;font-weight:700;white-space:nowrap;color:#374151;border-radius:8px 0 0 8px}
#panel td.c{text-align:center;padding:7px 2px;min-width:50px;font-weight:700;white-space:nowrap;
  border:2px solid #fff;border-radius:8px}
#panel td.d{background:#f3f4f6;color:#374151;font-weight:800;padding:5px 9px;font-size:11px}
#panel tr.r{cursor:pointer}
#panel tr.r:hover td.t{background:#f3f4f6}
#panel tr.sel td.t{background:#111827;color:#fff}
#panel tr.sel td.c{box-shadow:inset 0 0 0 2px #111827}
.lbl{background:#fff;border:0;border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600;
  color:#111827;box-shadow:0 1px 5px rgba(0,0,0,.35)}
.leaflet-tooltip-left:before,.leaflet-tooltip-right:before{display:none}
path.leaflet-interactive{filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}
@media (max-width:700px){
  #head{top:8px;left:8px;right:8px;width:auto;padding:9px 12px}
  #head .top{font-size:11px}
  #when{font-size:19px;margin:4px 0 6px}
  #sum{font-size:13px;padding:7px 9px}
  #tmp{margin-top:4px}
  #head .row{margin-top:8px}
  #head .row button{height:34px}
  #legend{margin-top:6px}
  .leaflet-control-zoom{display:none}
  #panel{top:auto;left:0;right:0;bottom:0;max-height:40vh;border-radius:16px 16px 0 0}
  #grab{display:block;text-align:center;font-size:12px;font-weight:700;color:#6b7280;padding:9px 0 7px;cursor:pointer}
  #panel.min #scroll{display:none}
  #panel th{font-size:10px;padding:7px 1px}
  #panel td.t{padding:8px 5px;font-size:12px}
  #panel td.c{min-width:0;padding:8px 1px;font-size:12px}
}
</style>
<div id="head" class="card">
  <div class="top"><span class="ttl">출근길 눈·비 예보</span><span id="upd">__UPDATED__</span></div>
  <div id="when"></div>
  <div id="sum"></div>
  <div id="tmp"></div>
  <div class="row"><button id="prev">◀</button><input type="range" id="rng"><button id="next">▶</button></div>
  <div id="legend">❄ 눈 &nbsp;🌨 비/눈 &nbsp;💧 비 &nbsp;· 없음 &nbsp;(숫자는 기온 ℃)</div>
</div>
<div id="panel" class="card">
  <div id="grab">▾ 시간별 표 접기</div>
  <div id="scroll"></div>
</div>
"""

JS = """
window.addEventListener('load', function() {
  var MAP = __MAP__;
  var PLACES = __PLACES__, TIMES = __TIMES__, KEYS = __KEYS__, TD = __TD__;
  var NAMES = __NAMES__, COLORS = __COLORS__, DIRS = __DIRS__;
  var ICON = {'3':'❄','7':'❄','2':'🌨','6':'🌨','1':'💧','4':'💧','5':'💧'};
  function fmt(t) { return (t === null || t === undefined) ? '' : String(t).replace('-', '−') + '°'; }

  L.control.zoom({position: 'bottomleft'}).addTo(MAP);
  var mk = [], rows = [];
  PLACES.forEach(function(p) {
    var c = L.circleMarker([p.lat, p.lon], {radius: 8, color: '#fff', weight: 2.5, fillOpacity: 1}).addTo(MAP);
    var left = DIRS[p.name] === 'left';
    c.bindTooltip('', {permanent: true, direction: left ? 'left' : 'right',
                       offset: [left ? -9 : 9, 0], className: 'lbl'});
    mk.push(c);
  });

  var tbl = document.createElement('table');
  var h = '<tr><th>시각</th>';
  PLACES.forEach(function(p) { h += '<th>' + p.name + '</th>'; });
  tbl.innerHTML = h + '</tr>';
  var lastDate = '';
  TD.forEach(function(t, i) {
    if (t[0] !== lastDate) {
      lastDate = t[0];
      var dc = tbl.insertRow().insertCell();
      dc.colSpan = PLACES.length + 1; dc.className = 'd'; dc.textContent = t[0];
    }
    var tr = tbl.insertRow(); tr.className = 'r';
    var c0 = tr.insertCell(); c0.className = 't'; c0.textContent = t[1];
    PLACES.forEach(function(p) {
      var code = p.codes[i], c = tr.insertCell(), tp = fmt(p.temps[i]);
      if (code === '?') tp = '?'; else if (!tp && code === '0') tp = '·';
      c.className = 'c';
      c.textContent = (ICON[code] ? ICON[code] + ' ' : '') + tp;
      c.style.background = (code === '0') ? '#f3f4f6' : COLORS[code];
      c.style.color = (code === '0') ? '#6b7280' : '#fff';
    });
    tr.onclick = function() { show(i); };
    rows.push(tr);
  });
  document.getElementById('scroll').appendChild(tbl);

  var panel = document.getElementById('panel'), head = document.getElementById('head');
  var rng = document.getElementById('rng'), sum = document.getElementById('sum');
  rng.min = 0; rng.max = TIMES.length - 1; rng.step = 1;
  var grab = document.getElementById('grab');
  grab.onclick = function() {
    var min = panel.classList.toggle('min');
    grab.textContent = min ? '▴ 시간별 표 보기' : '▾ 시간별 표 접기';
  };

  var cur = 0;
  function show(i) {
    i = Math.max(0, Math.min(TIMES.length - 1, i)); cur = i; rng.value = i;
    document.getElementById('when').textContent = TIMES[i];
    rows.forEach(function(r, k) { r.classList.toggle('sel', k === i); });
    if (rows[i].scrollIntoView) rows[i].scrollIntoView({block: 'nearest'});

    var g = {snow: [], mix: [], rain: [], unk: 0}, temps = [];
    PLACES.forEach(function(p, j) {
      var code = p.codes[i], tp = p.temps[i];
      if (code === '3' || code === '7') g.snow.push(p.name);
      else if (code === '2' || code === '6') g.mix.push(p.name);
      else if (code === '1' || code === '4' || code === '5') g.rain.push(p.name);
      else if (code === '?') g.unk++;
      if (tp !== null && tp !== undefined) temps.push(tp);
      mk[j].setStyle({fillColor: COLORS[code]});
      var state = ICON[code] ? NAMES[code] + ' ' : '';
      mk[j].setTooltipContent('<b>' + p.name + '</b> ' + state + fmt(tp));
    });
    var lines = [];
    if (g.snow.length) lines.push('❄ 눈 · ' + g.snow.join(', '));
    if (g.mix.length) lines.push('🌨 비/눈 · ' + g.mix.join(', '));
    if (g.rain.length) lines.push('💧 비 · ' + g.rain.join(', '));
    if (!lines.length) lines.push(g.unk === PLACES.length ? '자료 없음' : '✅ 비·눈 예보 없음');
    sum.className = g.snow.length ? 'snow' : g.mix.length ? 'mix' : g.rain.length ? 'rain' : 'clear';
    sum.innerHTML = lines.join('<br>');
    var tmp = document.getElementById('tmp');
    tmp.textContent = temps.length ? ('지점 기온 ' + fmt(Math.min.apply(null, temps)) + ' ~ ' + fmt(Math.max.apply(null, temps))) : '';
  }
  rng.oninput = function() { show(parseInt(rng.value)); };
  document.getElementById('prev').onclick = function() { show(cur - 1); };
  document.getElementById('next').onclick = function() { show(cur + 1); };

  // 열어 본 '지금' 시각(한국시간)에 맞는 줄을 처음에 선택
  function z(n) { return ('0' + n).slice(-2); }
  var d = new Date(Date.now() + 9 * 3600 * 1000);
  var nk = '' + d.getUTCFullYear() + z(d.getUTCMonth() + 1) + z(d.getUTCDate()) + z(d.getUTCHours());
  var start = KEYS.length - 1;
  for (var i = 0; i < KEYS.length; i++) { if (KEYS[i] >= nk) { start = i; break; } }
  show(start);

  // 핀 7개가 카드/표에 가려지지 않게 화면 맞춤
  try {
    var mobile = window.innerWidth <= 700;
    var b = L.latLngBounds(PLACES.map(function(p) { return [p.lat, p.lon]; }));
    MAP.fitBounds(b, {
      paddingTopLeft: [mobile ? 20 : head.offsetWidth + 50, mobile ? head.offsetHeight + 24 : 50],
      paddingBottomRight: [(mobile ? 20 : panel.offsetWidth + 40) + 80, mobile ? panel.offsetHeight + 24 : 50],
      maxZoom: 11
    });
  } catch (e) {}
});
"""


def main():
    now = now_kst()
    start = now.replace(minute=0, second=0, microsecond=0)
    limit = now + timedelta(hours=HOURS_AHEAD)
    s_date, s_time = latest_base(now)
    u_date, u_time = latest_ultra_base(now)
    print(f"현재(한국시간) {now:%m/%d %H:%M} / 단기예보 발표 {s_date} {s_time} / 초단기예보 발표 {u_date} {u_time}\n")
    if not SERVICE_KEY:
        print("인증키가 없습니다. key.txt 에 키를 저장하거나 SERVICE_KEY 환경변수를 설정하세요.")
        sys.exit(1)

    data = {}
    for name, (lat, lon) in PLACES.items():
        nx, ny = latlon_to_grid(lat, lon)
        try:
            merged = _fetch("getVilageFcst", nx, ny, s_date, s_time, 2000)
        except Exception as e:
            print(f"{name}: 조회 실패 ({e})")
            data[name] = {}
            continue
        try:  # 가까운 6시간은 초단기예보로 덮어쓰기 (더 정확)
            merged.update(_fetch("getUltraSrtFcst", nx, ny, u_date, u_time, 100))
        except Exception:
            pass
        per = {}
        for (d, t), (v, tmp) in merged.items():
            dt = datetime.strptime(d + t, "%Y%m%d%H%M")
            if start <= dt <= limit:
                per[dt.strftime("%Y%m%d%H")] = (v, to_temp(tmp))
        data[name] = per
        wet = [f"{hour_label(datetime.strptime(k, '%Y%m%d%H'))} {PTY_NAME[v]}({tp}°)"
               for k, (v, tp) in sorted(per.items()) if v != "0"]
        print(f"{name} (격자 {nx},{ny}): " + (", ".join(wet) if wet else "비/눈 예보 없음"))

    keys = sorted({k for per in data.values() for k in per})
    if not keys:
        print("\n받아온 예보가 없습니다. 인증키/신청 승인 여부를 확인하세요.")
        sys.exit(1)
    times = [hour_label(datetime.strptime(k, "%Y%m%d%H")) for k in keys]
    td = []
    for k in keys:
        d = datetime.strptime(k, "%Y%m%d%H")
        td.append([f"{d:%m/%d}({WEEK[d.weekday()]})", f"{d:%H}시"])
    places_js = [{"name": n, "lat": la, "lon": lo,
                  "codes": [data[n].get(k, ("?", None))[0] for k in keys],
                  "temps": [data[n].get(k, ("?", None))[1] for k in keys]}
                 for n, (la, lo) in PLACES.items()]

    m = folium.Map(
        location=[36.0, 127.55], zoom_start=9,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", zoom_control=False,
    )
    m.get_root().header.add_child(folium.Element('<meta http-equiv="refresh" content="600">'))
    m.get_root().html.add_child(folium.Element(UI.replace("__UPDATED__", f"예보 갱신 {now:%m/%d %H:%M} 기준")))
    js = (JS.replace("__PLACES__", json.dumps(places_js, ensure_ascii=False))
            .replace("__TIMES__", json.dumps(times, ensure_ascii=False))
            .replace("__KEYS__", json.dumps(keys))
            .replace("__TD__", json.dumps(td, ensure_ascii=False))
            .replace("__NAMES__", json.dumps(PTY_NAME, ensure_ascii=False))
            .replace("__COLORS__", json.dumps(PTY_COLOR))
            .replace("__DIRS__", json.dumps(LABEL_DIR, ensure_ascii=False))
            .replace("__MAP__", m.get_name()))
    m.get_root().html.add_child(folium.Element("<script>" + js + "</script>"))

    if IS_CI:
        os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
        out = os.path.join(HERE, "site", "index.html")
    else:
        out = os.path.join(HERE, "snow_map.html")
    m.save(out)
    print(f"\n{out} 저장 완료.")
    if not IS_CI:
        webbrowser.open("file://" + out)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        if IS_CI:
            sys.exit(1)
    if not IS_CI:
        input("\n엔터를 누르면 창이 닫힙니다...")
