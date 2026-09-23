#!/usr/bin/env python3
"""布局审计：只用 DOM 几何判定，不依赖任何视觉/截图判断。

为什么存在这个脚本
------------------
压字、溢出、裁切这类问题，截图肉眼经常看不出来（压的是文字行之间的
空隙），视觉模型的判断也不可靠——同一个问题它可能连续两次都说
"没有重叠"。本脚本把每一处判定都变成可复现的数值：矩形相交面积、
元素右边界与视口宽度之差、scrollWidth 与 clientWidth 之差。

判定标准全部是数值阈值，没有"看起来还行"。

用法
----
    python3 scripts/audit-layout.py                 # 全部视口
    python3 scripts/audit-layout.py 375 390         # 只跑这些视口宽度
    python3 scripts/audit-layout.py --reduced       # 额外跑 prefers-reduced-motion
    python3 scripts/audit-layout.py --json out.json

退出码 0 = 全部通过；1 = 存在 FAIL；2 = 环境/运行错误。
"""

import argparse
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

CHROME = "/Applications/Chromium.app/Contents/MacOS/Chromium"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 视口：覆盖最小安卓 / 各代 iPhone / iPad / 桌面，另加强迫矮高度用例
VIEWPORTS = [
    (320, 568, "iPhone SE 1"),
    (360, 640, "Android 小屏"),
    (375, 667, "iPhone 8/SE2"),
    (390, 844, "iPhone 12/13/14"),
    (414, 896, "iPhone XR/11"),
    (375, 480, "手机横屏矮高度"),
    (768, 1024, "iPad 竖屏"),
    (1024, 768, "iPad 横屏"),
    (1440, 900, "桌面"),
    (1920, 1080, "大桌面"),
]

# 需要检查相交的文本层组合。只比较"文字节点"的矩形，
# 容器矩形相交但文字不相交不算问题。
TEXT_PAIRS = [
    (".hud", ".titleblock", "HUD ↔ 左上标题"),
    (".hud", ".aside", "HUD ↔ 右上说明"),
    (".hud", "#readout", "HUD ↔ readout"),
    (".foot__grid", "#readout", "底部元数据 ↔ readout"),
    (".titleblock", ".aside", "左上标题 ↔ 右上说明"),
    (".foot__grid", ".axis-legend", "底部元数据 ↔ 轴图例"),
]

# 可交互元素的最小点击区域（CSS 像素）。SVG 档案点允许更小，只报 WARN。
MIN_TARGET = 32
MIN_TARGET_SVG = 16


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class Page:
    """一个 headless Chromium 页面，带 CDP 会话和静态文件服务。"""

    def __init__(self, width, height, reduced_motion=False):
        self.http_port = free_port()
        handler = functools.partial(Quiet, directory=ROOT)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.http_port), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        self.udir = tempfile.mkdtemp(prefix="tw-audit-")
        self.cdp_port = free_port()
        args = [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--mute-audio",
            f"--remote-debugging-port={self.cdp_port}", "--remote-allow-origins=*",
            f"--user-data-dir={self.udir}", "--no-first-run",
            f"--window-size={width},{height}", "--hide-scrollbars",
        ]
        if reduced_motion:
            args.append("--force-prefers-reduced-motion")
        args.append("about:blank")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.ws = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=5
                ) as r:
                    ws_url = json.load(r)["webSocketDebuggerUrl"]
                from websocket import create_connection

                self.ws = create_connection(ws_url, timeout=30)
                break
            except Exception:
                time.sleep(0.5)
        if not self.ws:
            self.close()
            raise RuntimeError("无法启动 headless Chromium")

        self._m = 0
        self.logs = []
        self.target = self._raw("Target.createTarget", url="about:blank")["targetId"]
        self.sid = self._raw(
            "Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self._s("Page.enable")
        self._s("Runtime.enable")
        self._s("Emulation.setDeviceMetricsOverride", width=width, height=height,
                deviceScaleFactor=1, mobile=width < 500)
        self._s("Page.navigate", url=f"http://127.0.0.1:{self.http_port}/index.html")
        time.sleep(4.6)

    # ---- CDP 传输 ----
    def _pump(self, want_id):
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == want_id:
                return msg
            m = msg.get("method")
            if m == "Runtime.consoleAPICalled":
                a = msg["params"]
                self.logs.append("console." + a["type"] + ": " + " ".join(
                    str(x.get("value", x.get("description", ""))) for x in a.get("args", [])))
            elif m == "Runtime.exceptionThrown":
                e = msg["params"]["exceptionDetails"]
                self.logs.append("exception: " + e.get("text", "") + " " +
                                 str(e.get("exception", {}).get("description", ""))[:200])

    def _raw(self, method, **params):
        self._m += 1
        self.ws.send(json.dumps({"id": self._m, "method": method, "params": params}))
        return self._pump(self._m).get("result", {})

    def _s(self, method, **params):
        self._m += 1
        self.ws.send(json.dumps({"id": self._m, "method": method, "params": params,
                                 "sessionId": self.sid}))
        return self._pump(self._m).get("result", {})

    def ev(self, expr):
        r = self._s("Runtime.evaluate", expression=expr, returnByValue=True)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            return {"__js_error": str(res.get("description"))[:300]}
        return res.get("value")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.udir, ignore_errors=True)


# 在页面里跑的测量脚本。所有判定数据一次取回。
MEASURE_JS = r"""(function(){
  function R(e){ if(!e) return null; var r=e.getBoundingClientRect();
    return {t:r.top,b:r.bottom,l:r.left,r:r.right,w:r.width,h:r.height}; }
  function txtRects(sel){
    return Array.prototype.slice.call(document.querySelectorAll(sel))
      .map(function(e){ return {el:e, r:R(e)}; })
      .filter(function(x){ return x.r && x.r.w>0 && x.r.h>0; });
  }
  var out = {innerW: innerWidth, innerH: innerHeight,
             docScrollW: document.documentElement.scrollWidth,
             phase: document.getElementById('phase').className};

  // 1) 视口横向溢出：任何可见元素边界超出视口
  //    只量"叶子节点"（无子元素的图形/text/path/circle...）。
  //    容器（如 <g class="pop p2">）的 getBoundingClientRect() 会按未变换前的
  //    局部坐标系算外接框，父级 rotate() 后容器框可能比真实像素大一圈 ——
  //    375px 视口下 g.pop.p2 量到 377.2，而其 4 个 rect 叶子最右只到 290.1。
  //    那不是真溢出，是容器框假象，按叶子判定才不会误报。
  var overflow = [];
  document.querySelectorAll('body *').forEach(function(e){
    if (e.children.length > 0) return;                     // 只量叶子
    if (e.closest('[aria-hidden="true"]')) return;         // 纯装饰层不计
    var cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if (parseFloat(cs.opacity) === 0) return;
    var r = R(e); if (!r || r.w === 0) return;
    if (r.r > innerWidth + 1 || r.l < -1) {
      overflow.push({sel: (e.getAttribute && (e.getAttribute('class') || e.tagName)) || String(e.className || e.tagName).slice(0,30),
                     right: Math.round(r.r), left: Math.round(r.l), w: Math.round(r.w)});
    }
  });
  out.overflow = overflow;

  // 2) 文本层相交（只比文字矩形）
  var pairs = __PAIRS__;
  out.textHits = [];
  pairs.forEach(function(p){
    var A = txtRects(p[0]), B = txtRects(p[1]);
    A.forEach(function(a){ B.forEach(function(b){
      var ox = Math.max(0, Math.min(a.r.r,b.r.r) - Math.max(a.r.l,b.r.l));
      var oy = Math.max(0, Math.min(a.r.b,b.r.b) - Math.max(a.r.t,b.r.t));
      if (ox > 0 && oy > 0) out.textHits.push({
        pair: p[2],
        a: String(a.el.className||a.el.tagName)+':'+a.el.textContent.trim().slice(0,18),
        b: String(b.el.className||b.el.tagName)+':'+b.el.textContent.trim().slice(0,18),
        ox: Math.round(ox), oy: Math.round(oy)});
    });});
  });

  // 3) 横向文字裁切
  //    只在元素真的会剪切时才报（overflow 是 hidden/clip/auto/scroll）。
  //    overflow:visible 的元素即使 scrollWidth > clientWidth 也不会掉一个像素 ——
  //    例如 .core 的底衬 ::before 有意比盒子宽 11px（那是垫出来的外边距，不是被切掉的字）。
  //    真正会剪的 .vol/.no/.zh/.date 都是 overflow:hidden，照旧会被这条抓到。
  var clipped = [];
  document.querySelectorAll('body *').forEach(function(e){
    if (e.closest('[aria-hidden="true"]')) return;
    var cs = getComputedStyle(e);
    if (cs.display === 'none') return;
    var ov = cs.overflowX;
    if (!(ov === 'hidden' || ov === 'clip' || ov === 'auto' || ov === 'scroll')) return;
    if (e.scrollWidth > e.clientWidth + 1 && e.clientWidth > 0) {
      var t = (e.textContent||'').trim();
      if (t) clipped.push({sel: String(e.className||e.tagName).slice(0,30), text: t.slice(0,24),
                           scrollW: e.scrollWidth, clientW: e.clientWidth});
    }
  });
  out.clipped = clipped;

  // 4) 可交互元素点击区域
  var targets = [];
  document.querySelectorAll('button, [role="button"], .pt').forEach(function(e){
    if (e.closest('[aria-hidden="true"]')) return;
    var cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    var r = R(e); if (!r) return;
    // 第一幕/复位时档案点被 transform:scale(0) 收起（opacity:0），
    // 此时测量它没有意义，跳过。注意：只查 opacity 不够 —— 复位时 opacity 已经是 0，
    // 但 scale(0) 过渡还没跑完，getBoundingClientRect() 量到的是收缩中间态
    // （如 1x1px / 6x6px / 13x13px），会误报"点击区过小"。
    // transform 未复位（含 matrix(...) 里的缩放分量 ≠ 1）时同样跳过。
    if (e.classList.contains('pt')) {
      if (parseFloat(cs.opacity) === 0) return;
      var m = cs.transform && cs.transform !== 'none'
        ? cs.transform.match(/matrix\(([^,]+),([^,]+)/) : null;
      if (m && (Math.abs(parseFloat(m[1]) - 1) > 0.01 || Math.abs(parseFloat(m[2])) > 0.01)) return;
    }
    var min = e.classList.contains('pt') ? __MINSVG__ : __MINTGT__;
    if (r.w < min || r.h < min) targets.push({
      sel: String(e.className||e.tagName).slice(0,30),
      w: Math.round(r.w), h: Math.round(r.h), min: min});
  });
  out.targets = targets;

  // 5) 结构位置
  var st = R(document.querySelector('.stage')), ft = R(document.querySelector('.foot'));
  var pts = Array.prototype.slice.call(document.querySelectorAll('.pt'));
  out.ptsTotal = pts.length;
  out.ptsOutOfStage = pts.filter(function(p){
    var q = R(p); return !q || q.t < st.t-2 || q.b > st.b+2 || q.l < st.l-2 || q.r > st.r+2;
  }).length;
  var ro = R(document.getElementById('readout'));
  out.readoutVsFootTop = ro ? Math.round(ro.b - ft.t) : null;   // >0 即越过 foot 顶边
  var core = R(document.querySelector('.core')), ring = R(document.querySelector('.ringwrap'));
  out.coreVsRing = (core && ring) ? {
    top: Math.round(core.t - ring.t), bottom: Math.round(ring.b - core.b),
    left: Math.round(core.l - ring.l), right: Math.round(ring.r - core.r)} : null;
  out.ringInStage = ring ? (ring.b <= ft.t + 1 && ring.t >= 0) : null;
  return out;
})()"""


def measure(page):
    js = (MEASURE_JS.replace("__PAIRS__", json.dumps([[a, b, c] for a, b, c in TEXT_PAIRS]))
          .replace("__MINSVG__", str(MIN_TARGET_SVG)).replace("__MINTGT__", str(MIN_TARGET)))
    return page.ev(js)


def judge(m):
    """把测量结果变成 PASS/FAIL/WARN 列表。"""
    if "__js_error" in m:
        return [("FAIL", "页面 JS 执行失败: " + m["__js_error"])]

    res = []

    if m["overflow"]:
        for o in m["overflow"][:6]:
            res.append(("FAIL", f"横向溢出 {o['sel']} right={o['right']} "
                                f"(视口 {m['innerW']})"))
    else:
        res.append(("PASS", "无横向溢出"))

    if m["textHits"]:
        for h in m["textHits"][:8]:
            res.append(("FAIL", f"文字相交 {h['pair']}: 「{h['a']}」×「{h['b']}」 "
                                f"{h['ox']}x{h['oy']}px"))
    else:
        res.append(("PASS", "文本层无相交"))

    if m["clipped"]:
        for c in m["clipped"][:6]:
            res.append(("FAIL", f"文字被裁切 {c['sel']} 「{c['text']}」 "
                                f"scrollW={c['scrollW']} clientW={c['clientW']}"))
    else:
        res.append(("PASS", "无横向裁切"))

    fail = [t for t in m["targets"] if t["min"] != MIN_TARGET_SVG]
    warn = [t for t in m["targets"] if t["min"] == MIN_TARGET_SVG]
    for t in fail[:6]:
        res.append(("FAIL", f"点击区域过小 {t['sel']} {t['w']}x{t['h']} < {t['min']}"))
    for t in warn[:4]:
        res.append(("WARN", f"档案点点击区 {t['w']}x{t['h']}px（触屏难以点中）"))
    if not fail:
        res.append(("PASS", "按钮点击区域达标"))

    if m["ptsOutOfStage"]:
        res.append(("FAIL", f"{m['ptsOutOfStage']}/{m['ptsTotal']} 个档案点越出舞台"))
    else:
        res.append(("PASS", f"{m['ptsTotal']} 个档案点均在舞台内"))

    if m["readoutVsFootTop"] is not None and m["readoutVsFootTop"] > 0:
        res.append(("FAIL", f"readout 底边越过 foot 顶边 {m['readoutVsFootTop']}px"))
    else:
        res.append(("PASS", "readout 未压到底部面板"))

    if m["docScrollW"] > m["innerW"] + 1:
        res.append(("FAIL", f"文档产生横向滚动 {m['docScrollW']} > {m['innerW']}"))

    return res


def audit(width, height, label, reduced_motion=False):
    page = Page(width, height, reduced_motion)
    results = []
    try:
        # ---- 第一幕：封面 ----
        results.append((label, "封面", judge(measure(page))))

        # ---- 第二幕：坐标目录 ----
        # 转场总长 ~3.5s（舒缓化后拉长），等它跑完再测量，
        # 否则会量到中途状态（档案点还没落位）而误报。
        page.ev("document.getElementById('btnEnter').click()")
        time.sleep(4.4)
        results.append((label, "目录", judge(measure(page))))

        # ---- 交互：悬停第 5 点 + 键盘序列 ----
        ia = page.ev(r"""(function(){
          document.querySelectorAll('.pt')[4].dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));
          var a = document.getElementById('roId').textContent;
          document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
          var b = document.getElementById('roId').textContent;
          document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
          return a + '>' + b;
        })()""")
        results.append((label, "交互", [(
            "PASS" if ia == "AI-005>AI-006" else "FAIL",
            f"悬停/键盘序列 = {ia}（期望 AI-005>AI-006）")]))

        # ---- 返回封面后状态 ----
        # .pt 收起有 .32s transition，等它跑完再测量，
        # 否则会量到 scale() 中间态而误报点击区过小。
        time.sleep(3.4)
        extra = []
        ph = page.ev("document.getElementById('phase').className")
        extra.append(("PASS" if ph == "phase" else "FAIL", f"Esc 后回到封面：{ph}"))
        extra += judge(measure(page))
        results.append((label, "复位", extra))

        # ---- 控制台 ----
        bad = [l for l in page.logs if l.startswith(("exception", "console.error"))]
        results.append((label, "控制台", [("FAIL", l[:160]) for l in bad[:4]] or
                        [("PASS", "无错误日志")]))
    finally:
        page.close()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("widths", nargs="*", type=int)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--reduced", action="store_true", help="额外跑一遍 prefers-reduced-motion")
    args = ap.parse_args()

    vps = VIEWPORTS
    if args.widths:
        vps = [v for v in VIEWPORTS if v[0] in args.widths]
        if not vps:
            vps = [(w, 800, f"{w}px") for w in args.widths]

    all_res = []
    for w, h, label in vps:
        print(f"… 审计 {label} {w}x{h}")
        all_res += audit(w, h, label)
    if args.reduced:
        all_res += audit(375, 667, "reduce-motion", reduced_motion=True)

    fails = warns = 0
    print()
    print("=" * 78)
    for label, phase, items in all_res:
        bad = [i for i in items if i[0] != "PASS"]
        print(f"[{'OK  ' if not bad else 'FAIL'}] {label:<16} {phase}")
        for st, msg in items:
            if st == "PASS":
                continue
            print(f"        {st}: {msg}")
            if st == "FAIL":
                fails += 1
            else:
                warns += 1
    print("=" * 78)
    print(f"FAIL={fails}  WARN={warns}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump([{"viewport": a, "phase": b, "items": c} for a, b, c in all_res],
                      f, ensure_ascii=False, indent=1)
        print("JSON ->", args.json_out)
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("运行错误:", e)
        sys.exit(2)
