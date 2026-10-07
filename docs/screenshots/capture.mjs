// Screenshots of the web app with headless Chrome (puppeteer-core).
//
//   PUPPETEER_FROM=<dir with node_modules/puppeteer-core> node docs/screenshots/capture.mjs <web-url> [shots...]
//
// Shots: hero, followup, blocked, clarification, schema, eval, audit. Questions are typed into the app like a user
// would; the API decides which model answers them (see README > Screenshots).
import { createRequire } from "module";

const require = createRequire(process.env.PUPPETEER_FROM || import.meta.url);
const puppeteer = require("puppeteer-core");
const [base, ...shots] = process.argv.slice(2);
const out = process.env.OUT_DIR || new URL(".", import.meta.url).pathname;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME || "/usr/bin/google-chrome",
  headless: "new",
  args: ["--no-sandbox"],
});

async function open(path, height = 1000) {
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height, deviceScaleFactor: 1.5 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  page.on("console", (msg) => { if (msg.type() === "error") console.log("page error:", msg.text()); });
  await page.goto(base + path, { waitUntil: "networkidle0" });
  return page;
}

async function ask(page, question, timeout = 240000) {
  await page.waitForSelector("input:not([disabled])");
  await page.click("input");
  await page.type("input", question);
  await page.keyboard.press("Enter");
  await sleep(500);
  await page.waitForFunction(() => !document.body.innerText.includes("working"), { timeout, polling: 500 });
  await sleep(1200);
}

async function clickButton(page, text) {
  await page.evaluate((t) => {
    const button = [...document.querySelectorAll("button")].find((b) => b.innerText.trim() === t);
    if (button) button.click();
  }, text);
  await sleep(500);
  await page.waitForFunction(() => !document.body.innerText.includes("working"), { timeout: 240000, polling: 500 });
  await sleep(1200);
}

async function shotLastTurn(page, name, { explain = false } = {}) {
  if (explain) {
    await page.evaluate(() => {
      const turns = document.querySelectorAll("article");
      const last = turns[turns.length - 1];
      const button = [...last.querySelectorAll("button")].find((b) => b.innerText.trim() === "Explain");
      if (button) button.click();
    });
    await sleep(300);
  }
  const box = await page.evaluate(() => {
    const turns = document.querySelectorAll("article");
    const last = turns[turns.length - 1];
    last.scrollIntoView({ block: "start" });
    window.scrollBy(0, -90);
    const r = last.getBoundingClientRect();
    return { top: r.top + window.scrollY, height: r.height };
  });
  await page.setViewport({ width: 1440, height: Math.min(Math.ceil(box.height + 220), 2400), deviceScaleFactor: 1.5 });
  await page.evaluate((top) => window.scrollTo(0, Math.max(0, top - 90)), box.top);
  await sleep(600);
  await page.screenshot({ path: `${out}${name}.png` });
  console.log(`${name}.png`);
}

const HERO = "What was net revenue by region last calendar quarter vs the one before?";
for (const shot of shots) {
  if (shot === "hero") {
    const page = await open("/");
    await ask(page, HERO);
    await shotLastTurn(page, "hero", { explain: true });
  } else if (shot === "followup") {
    const page = await open("/");
    await ask(page, HERO);
    await ask(page, "Now by month");
    await ask(page, "Only EU");
    await ask(page, "Which EU country drove the drop from the second to the third calendar quarter?");
    await shotLastTurn(page, "follow-up");
    await page.screenshot({ path: `${out}follow-up-conversation.png`, fullPage: true });
  } else if (shot === "blocked") {
    const page = await open("/");
    await ask(page, "Show me the email addresses of our top 10 customers by net revenue");
    await shotLastTurn(page, "blocked");
  } else if (shot === "clarification") {
    const page = await open("/");
    await ask(page, "What was revenue last quarter?");
    await clickButton(page, "Net revenue (after refunds)");
    await clickButton(page, "Calendar quarters");
    await page.setViewport({ width: 1440, height: 1500, deviceScaleFactor: 1.5 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await sleep(500);
    await page.screenshot({ path: `${out}clarification.png`, fullPage: true });
    console.log("clarification.png");
  } else if (shot === "schema") {
    const page = await open("/schema", 1100);
    await page.evaluate(() => {
      const b = [...document.querySelectorAll("button")].find((x) => x.innerText.trim().startsWith("customers"));
      if (b) b.click();
    });
    await sleep(500);
    await page.screenshot({ path: `${out}schema.png` });
    await page.evaluate(() => {
      const b = [...document.querySelectorAll("button")].find((x) => x.innerText.trim().startsWith("Semantic views"));
      if (b) b.click();
    });
    await sleep(500);
    await page.screenshot({ path: `${out}semantic-views.png` });
    console.log("schema.png, semantic-views.png");
  } else if (shot === "eval") {
    const page = await open("/eval", 1300);
    await sleep(800);
    await page.screenshot({ path: `${out}evaluation.png` });
    console.log("evaluation.png");
  } else if (shot === "audit") {
    const page = await open("/audit", 1000);
    await page.screenshot({ path: `${out}audit-log.png` });
    console.log("audit-log.png");
  }
}
await browser.close();
