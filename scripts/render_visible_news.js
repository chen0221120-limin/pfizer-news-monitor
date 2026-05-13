const fs = require("fs");
const path = require("path");

async function launchBrowser(chromium) {
  const attempts = [
    () => chromium.launch({ headless: true }),
    () => chromium.launch({ headless: true, channel: "msedge" }),
    () => chromium.launch({ headless: true, channel: "chrome" }),
  ];
  let lastError = null;
  for (const attempt of attempts) {
    try {
      return await attempt();
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

async function main() {
  const [url, outputPath, screenshotPath, timeoutArg] = process.argv.slice(2);
  if (!url || !outputPath || !screenshotPath) {
    throw new Error("Usage: node render_visible_news.js <url> <output-json> <screenshot-path> [timeout-ms]");
  }

  const timeout = Number(timeoutArg || 25000);
  const { chromium } = require("playwright");
  const browser = await launchBrowser(chromium);

  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 2200 } });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout });
    await page.waitForTimeout(1800);
    await page.screenshot({ path: screenshotPath, fullPage: true });

    const items = await page.evaluate(() => {
      const visible = (element) => {
        if (!element) return false;
        const style = window.getComputedStyle(element);
        if (style.display === "none" || style.visibility === "hidden") return false;
        const rect = element.getBoundingClientRect();
        return rect.width > 8 && rect.height > 8;
      };

      const parseDate = (text) => {
        if (!text) return "";
        const normalized = text.replace(/\s+/g, " ").trim();
        const patterns = [
          /\b\d{4}-\d{2}-\d{2}\b/,
          /\b\d{4}\/\d{2}\/\d{2}\b/,
          /\b[A-Z][a-z]+ \d{1,2}, \d{4}\b/,
          /\b\d{1,2} [A-Z][a-z]+ \d{4}\b/,
          /\b\d{1,2} [A-Z][a-z]{2} \d{4}\b/,
        ];
        for (const pattern of patterns) {
          const match = normalized.match(pattern);
          if (match) return match[0];
        }
        return "";
      };

      const anchors = Array.from(document.querySelectorAll("a[href]"));
      const rows = [];

      for (const anchor of anchors) {
        const href = anchor.href || "";
        const title = (anchor.textContent || "").replace(/\s+/g, " ").trim();
        if (!visible(anchor)) continue;
        if (!href.startsWith("http")) continue;
        if (title.length < 12 || title.length > 260) continue;

        const container = anchor.closest("article, li, section, div");
        const containerText = ((container && container.innerText) || anchor.innerText || "").replace(/\s+/g, " ").trim();
        const dateText = parseDate(containerText);
        if (!dateText) continue;

        rows.push({
          title,
          url: href.split("#")[0],
          date: dateText,
          snippet: containerText.slice(0, 320),
        });
      }

      const unique = [];
      const seen = new Set();
      for (const row of rows) {
        const key = `${row.url}||${row.title}`;
        if (seen.has(key)) continue;
        seen.add(key);
        unique.push(row);
      }
      return unique.slice(0, 40);
    });

    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(outputPath, JSON.stringify(items, null, 2), "utf8");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
