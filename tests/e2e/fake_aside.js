// 시험용 가짜 'aside' CLI: Aside CLI 1.26 의 repl 과 같은 전역(openTab·closeTab·sleep·pwd·fs·path·Buffer)으로
// 스크립트를 진짜 크로미움(Playwright)에서 돌린다. 종료 코드는 늘 0, 실패는 '[error' 줄 (진짜 Aside 와 같음).
const { chromium } = require("playwright");
const fs = require("fs"), path = require("path"), os = require("os");
(async () => {
  const args = process.argv.slice(2);
  if (args[0] === "--version") { console.log("aside 1.26.0 (fake)"); return; }
  if (args[0] !== "repl") { console.log("[error unknown command"); return; }
  const pwd = fs.mkdtempSync(path.join(os.tmpdir(), "aside-"));
  process.chdir(pwd);                                   // 상대 경로 사진은 세션 폴더에
  const browser = await chromium.launch(process.env.FAKE_ASIDE_CHROMIUM ? { executablePath: process.env.FAKE_ASIDE_CHROMIUM } : {});
  const context = await browser.newContext();
  const openTab = async url => {
    const pg = await context.newPage();
    const cdp = await context.newCDPSession(pg);
    await cdp.send("Page.enable");                     // (진짜 Aside 처럼 새 문서 스크립트가 먹게)
    pg._sendToTarget = (m, p) => cdp.send(m, p);
    if (url && url !== "about:blank") await pg.goto(url);
    return pg;
  };
  const closeTab = async pg => { await pg.close(); };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    await new AsyncFunction("openTab", "closeTab", "sleep", "pwd", "fs", "path", "Buffer", args[1])(
      openTab, closeTab, sleep, pwd, fs.promises, path, Buffer);
    console.log("[ok");
  } catch (e) {
    console.log("[error " + String(e && e.message || e).slice(0, 300));
  }
  await browser.close();
})();
