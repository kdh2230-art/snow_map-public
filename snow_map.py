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

PTY_NAME = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "4": "소나기",
            "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림", "?": "자료 없음"}
PTY_COLOR = {"0": "#9e9e9e", "1": "#2ecc71", "2": "#8e44ad", "3": "#1e6fff",
             "4": "#2ecc71", "5": "#2ecc71", "6": "#8e44ad", "7": "#1e6fff", "?": "#333333"}
SHORT = {"0": "·", "1": "비", "2": "비눈", "3": "눈", "4": "소나기",
         "5": "빗방울", "6": "빗방울눈", "7": "눈날림", "?": "?"}
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
            return {(i["fcstDate"], i["fcstTime"]): i["fcstValue"] for i in items if i["category"] == "PTY"}
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def hour_label(dt):
    return f"{dt:%m/%d}({WEEK[dt.weekday()]}) {dt:%H}시"


UI = """
<style>
#ctl{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);z-index:9999;background:#fff;
  padding:8px 12px;border:1px solid #888;border-radius:8px;font-size:14px;text-align:center}
#ctl button{font-size:16px;padding:2px 10px}
#ctl select{font-size:15px}
#rng{width:300px;margin-top:6px}
#panel{position:fixed;top:10px;right:10px;z-index:9999;background:rgba(255,255,255,.96);
  border:1px solid #888;border-radius:8px;max-height:calc(100vh - 20px);overflow:auto}
#upd{font-size:11px;color:#666;padding:4px 8px}
#legend{position:fixed;bottom:30px;left:30px;z-index:9999;background:#fff;padding:10px;
  border:1px solid #888;font-size:13px}
#panel table{border-collapse:collapse;font-size:12px}
#panel th{position:sticky;top:0;background:#fff;z-index:2;padding:3px;font-size:11px}
#panel td{padding:2px 3px}
#panel td.t{padding:2px 6px;font-weight:bold;white-space:nowrap}
#panel td.c{text-align:center;min-width:46px;border:1px solid #fff}
#panel td.d{background:#333;color:#fff;font-weight:bold;padding:2px 6px}
@media (max-width:700px){
  #ctl{top:8px;bottom:auto;left:56px;right:8px;transform:none;padding:6px 8px;font-size:13px}
  #rng{width:100%;margin-top:4px}
  #ctl select{font-size:14px;max-width:45vw}
  #panel{top:auto;bottom:0;left:0;right:0;max-height:42vh;border-radius:10px 10px 0 0;
         border-left:0;border-right:0;border-bottom:0}
  #panel table{width:100%}
  #panel th{font-size:10px;padding:4px 1px}
  #panel td{padding:5px 1px;font-size:13px}
  #panel td.t{padding:5px 4px;font-size:12px}
  #panel td.c{min-width:0}
  #legend{display:none}
}
</style>
<div id="ctl">
  <button id="prev">◀</button>
  <select id="sel"></select>
  <button id="next">▶</button><br>
  <input type="range" id="rng">
</div>
<div id="panel"><div id="upd">__UPDATED__</div></div>
<div id="legend">
  <b>그 시각 예보</b><br>
  <span style="color:#1e6fff">●</span> 눈 / 눈날림<br>
  <span style="color:#8e44ad">●</span> 비/눈<br>
  <span style="color:#2ecc71">●</span> 비 / 소나기 / 빗방울<br>
  <span style="color:#9e9e9e">●</span> 없음<br>
  <span style="color:#333">●</span> 자료 없음
</div>
"""

JS = """
window.addEventListener('load', function() {
  var MAP = __MAP__;
  var PLACES = __PLACES__, TIMES = __TIMES__, KEYS = __KEYS__, TD = __TD__;
  var NAMES = __NAMES__, COLORS = __COLORS__, SHORT = __SHORT__;
  var mk = [], rows = [];
  PLACES.forEach(function(p) {
    var c = L.circleMarker([p.lat, p.lon], {radius: 15, color: '#222', weight: 2, fillOpacity: 0.92}).addTo(MAP);
    c.bindTooltip('', {permanent: true, direction: 'right', offset: [14, 0]});
    mk.push(c);
  });

  var panel = document.getElementById('panel');
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
    var tr = tbl.insertRow();
    tr.style.cursor = 'pointer';
    var c0 = tr.insertCell(); c0.className = 't'; c0.textContent = t[1];
    PLACES.forEach(function(p) {
      var code = p.codes[i], c = tr.insertCell();
      c.className = 'c'; c.textContent = SHORT[code];
      c.style.background = (code === '0') ? '#f1f1f1' : COLORS[code];
      c.style.color = (code === '0') ? '#bbb' : '#fff';
      if (code !== '0') c.style.fontWeight = 'bold';
    });
    tr.onclick = function() { show(i); };
    rows.push(tr);
  });
  panel.appendChild(tbl);

  var sel = document.getElementById('sel'), rng = document.getElementById('rng');
  TIMES.forEach(function(t, i) { var o = document.createElement('option'); o.value = i; o.text = t; sel.appendChild(o); });
  rng.min = 0; rng.max = TIMES.length - 1; rng.step = 1;

  function show(i) {
    i = Math.max(0, Math.min(TIMES.length - 1, i));
    sel.value = i; rng.value = i;
    rows.forEach(function(r, k) { r.style.outline = (k === i) ? '3px solid #000' : 'none'; r.style.outlineOffset = '-3px'; });
    if (rows[i].scrollIntoView) rows[i].scrollIntoView({block: 'nearest'});
    PLACES.forEach(function(p, j) {
      var code = p.codes[i];
      mk[j].setStyle({fillColor: COLORS[code]});
      mk[j].setTooltipContent('<b>' + p.name + '</b> ' + NAMES[code]);
    });
  }
  sel.onchange = function() { show(parseInt(sel.value)); };
  rng.oninput = function() { show(parseInt(rng.value)); };
  document.getElementById('prev').onclick = function() { show(parseInt(sel.value) - 1); };
  document.getElementById('next').onclick = function() { show(parseInt(sel.value) + 1); };

  // 열어 본 '지금' 시각(한국시간)에 맞는 줄을 처음에 선택
  function z(n) { return ('0' + n).slice(-2); }
  var d = new Date(Date.now() + 9 * 3600 * 1000);
  var nk = '' + d.getUTCFullYear() + z(d.getUTCMonth() + 1) + z(d.getUTCDate()) + z(d.getUTCHours());
  var start = KEYS.length - 1;
  for (var i = 0; i < KEYS.length; i++) { if (KEYS[i] >= nk) { start = i; break; } }
  show(start);

  // 핀 7개가 화면에 다 보이게 맞춤 (표/조작바에 가려지지 않게 여백)
  try {
    var mobile = window.innerWidth <= 700;
    var b = L.latLngBounds(PLACES.map(function(p) { return [p.lat, p.lon]; }));
    MAP.fitBounds(b, {
      paddingTopLeft: [20, mobile ? 70 : 40],
      paddingBottomRight: [(mobile ? 20 : panel.offsetWidth + 30) + 90,
                           mobile ? Math.round(window.innerHeight * 0.42) + 20 : 90],
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
        for (d, t), v in merged.items():
            dt = datetime.strptime(d + t, "%Y%m%d%H%M")
            if start <= dt <= limit:
                per[dt.strftime("%Y%m%d%H")] = v
        data[name] = per
        wet = [f"{hour_label(datetime.strptime(k, '%Y%m%d%H'))} {PTY_NAME[v]}"
               for k, v in sorted(per.items()) if v != "0"]
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
                  "codes": [data[n].get(k, "?") for k in keys]} for n, (la, lo) in PLACES.items()]

    m = folium.Map(
        location=[36.0, 127.55], zoom_start=9,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
    )
    m.get_root().header.add_child(folium.Element('<meta http-equiv="refresh" content="1800">'))
    m.get_root().html.add_child(folium.Element(UI.replace("__UPDATED__", f"예보 갱신 {now:%m/%d %H:%M} 기준")))
    js = (JS.replace("__PLACES__", json.dumps(places_js, ensure_ascii=False))
            .replace("__TIMES__", json.dumps(times, ensure_ascii=False))
            .replace("__KEYS__", json.dumps(keys))
            .replace("__TD__", json.dumps(td, ensure_ascii=False))
            .replace("__NAMES__", json.dumps(PTY_NAME, ensure_ascii=False))
            .replace("__COLORS__", json.dumps(PTY_COLOR))
            .replace("__SHORT__", json.dumps(SHORT, ensure_ascii=False))
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
