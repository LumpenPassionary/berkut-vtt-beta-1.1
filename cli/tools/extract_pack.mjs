#!/usr/bin/env node
/**
 * Распаковать LevelDB-пак Foundry VTT в JSONL: одна строка — один документ.
 *
 * Использование:
 *   node extract_pack.mjs <папка_пака> <выходной.jsonl>
 *
 * Пак читается только на чтение (createIfMissing: false), LOCK не трогаем:
 * если Foundry запущен и держит пак, LevelDB просто не откроется — это
 * понятная ошибка, а не повреждение данных.
 */
import fs from "node:fs";
import path from "node:path";
import { ClassicLevel } from "classic-level";

const [, , packDir, outFile] = process.argv;
if (!packDir || !outFile) {
  console.error("Usage: node extract_pack.mjs <packDir> <outFile.jsonl>");
  process.exit(2);
}
if (!fs.existsSync(packDir)) {
  console.error(`Пак не найден: ${packDir}`);
  process.exit(2);
}

const db = new ClassicLevel(packDir, { createIfMissing: false, keyEncoding: "buffer", valueEncoding: "buffer" });
let count = 0;
let out = "";
try {
  await db.open();
  for await (const [key, value] of db.iterator()) {
    let doc;
    try {
      doc = JSON.parse(value.toString("utf8"));
    } catch (err) {
      console.error(`Битая запись ${key.toString("utf8")}: ${err.message}`);
      continue;
    }
    out += JSON.stringify({ _packKey: key.toString("utf8"), ...doc }) + "\n";
    count++;
  }
  await db.close();
} catch (err) {
  console.error(`Не удалось прочитать пак ${packDir}: ${err.message}`);
  process.exit(1);
}
fs.mkdirSync(path.dirname(outFile), { recursive: true });
fs.writeFileSync(outFile, out, "utf8");
console.log(JSON.stringify({ pack: path.basename(packDir), documents: count }));
