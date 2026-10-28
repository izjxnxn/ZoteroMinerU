const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path").win32;

const token = "a".repeat(43);
const work = "C:\\app\\work";
const markdown = path.join(work, "PARENT01", "MD_Liu 等_Title.md");
const linked = path.join("C:\\out", "MD_Liu 等_Title.md");
const summary = path.join(work, "summaries", "PARENT01", "SUM_Liu 等_Title.md");
const bilingual = path.join(work, "translations", "PARENT01", "out", "双语_Liu 等_Title.pdf");
const mono = path.join(work, "translations", "PARENT01", "out", "译文_Liu 等_Title.pdf");
const disguised = path.join(work, "translations", "PARENT01", "out", "双语_Liu 等_Title.md.exe");
const existingFiles = new Set([markdown, linked, summary, bilingual, mono, disguised, path.join(work, "PARENT02", "MD_x.md")]);
const notes = [];
const trashedIds = [];
let nextID = 100;
const items = new Map();
function attachment(key, parentID, filePath, type = "text/markdown") {
    const value = { id: nextID++, key, parentID, deleted: false, attachmentContentType: type,
        get attachmentFilename() { return path.basename(filePath); }, isAttachment: () => true, isNote: () => false,
        isRegularItem: () => false, getFilePathAsync: async () => filePath, saved: 0,
        async saveTx() { this.saved++; } };
    items.set(value.id, value);
    return value;
}
function regular(key) {
    const value = { id: nextID++, key, deleted: false, isRegularItem: () => true,
        getAttachments: () => [...items.values()].filter(i => i.parentID === value.id).map(i => i.id) };
    items.set(value.id, value);
    return value;
}
const parent = regular("PARENT01");
attachment("PDFKEY01", parent.id, "C:\\Zotero\\storage\\PDFKEY01\\a.pdf", "application/pdf");
const second = regular("PARENT02");
const legacy = attachment("LEGACY02", second.id, "C:\\old\\full.md");
const calls = [];
let observer;
const context = {
    PathUtils: { join: path.join }, IOUtils: { readJSON: async () => ({ token, allowed_roots: [work, "C:\\out\\"] }) },
    Services: { env: { get: () => "C:\\app" } },
    Zotero: {
        isWin: true, version: "9.0.6", DataDirectory: { dir: "C:\\Zotero" }, debug() {}, logError() {},
        Notifier: { registerObserver(value) { observer = value; return "observer"; }, unregisterObserver() {} },
        Item: class { constructor(type) { this.type = type; this.key = "NOTE0001"; }
            setNote(value) { this.html = value; } async saveTx() { notes.push(this); } },
        Server: { Endpoints: {} }, Libraries: { userLibraryID: 1, get: () => ({ editable: true }) },
        File: { pathToFile(value) {
            if (typeof value !== "string" || !path.isAbsolute(value)) throw new Error("absolute path required");
            return { path: value, normalize() { this.path = path.normalize(this.path); },
                exists() { return existingFiles.has(this.path); }, isFile: () => true,
                get leafName() { return path.basename(this.path); } };
        } },
        Items: {
            getByLibraryAndKeyAsync: async (_, key) => [...items.values()].find(i => i.key === key) || null,
            getAsync: async id => items.get(id),
            async trashTx(ids) { for (const id of ids) items.get(id).deleted = true; trashedIds.push(...ids); },
        },
        Attachments: {
            getFileBaseNameFromItem(item, options) {
                assert.equal(options.formatString, '{{ firstCreator suffix="_" }}{{ title truncate="100" }}');
                return "Liu 等_Title " + item.key;
            },
            async importFromFile(options) { calls.push(["import", options]); return attachment("NEWMD00" + calls.length, options.parentItemID, "C:\\Zotero\\storage\\X\\" + path.basename(options.file), options.contentType); },
            async linkFromFile(options) { calls.push(["link", options]); return attachment("NEWMD00" + calls.length, options.parentItemID, options.file, options.contentType); },
        },
    },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require("node:path").join(__dirname, "../bridge/bridge.js"), "utf8"), context);
const bridge = context.MineruBridge;
const request = (data, extra = {}) => ({ data, headers: { "x-mineru-bridge-token": token, ...extra } });
const body = result => JSON.parse(result[2]);
const attach = { op: "attach", parent_key: "PARENT01", file: markdown, mode: "import", title: "MD_Liu 等_Title" };

(async () => {
    bridge.start();
    assert(context.Zotero.Server.Endpoints["/mineru-bridge/v1"]);
    let result = await bridge.handle(request({ op: "status" }));
    assert.equal(result[0], 200);
    assert.equal(body(result).protocol, 5);
    observer.notify();
    assert.equal(bridge.revision, 1);
    result = await bridge.handle({ data: { op: "status" }, headers: { get: name => name.toLowerCase() === "x-mineru-bridge-token" ? token : null } });
    assert.equal(result[0], 200);
    assert.equal((await bridge.handle(request({ op: "status" }, { "x-mineru-bridge-token": "wrong" })))[0], 403);
    assert.equal((await bridge.handle(request({ op: "status" }, { origin: "https://untrusted.example" })))[0], 403);
    assert.equal((await bridge.handle(request({ op: "eval", code: "throw 1" })))[0], 400);

    result = await bridge.handle(request({ op: "names", keys: ["PARENT01", "PDFKEY01"] }));
    assert.deepEqual(body(result).names, { PARENT01: "Liu 等_Title PARENT01" });
    assert.equal((await bridge.handle(request({ op: "names", keys: ["bad key"] })))[0], 400);

    assert.equal((await bridge.handle(request({ ...attach, parent_key: "MISSING1" })))[0], 400);
    assert.equal((await bridge.handle(request({ ...attach, file: "C:\\outside\\MD_a.md" })))[0], 400);
    assert.equal((await bridge.handle(request({ ...attach, file: "C:\\app\\workevil\\MD_a.md" })))[0], 400);
    assert.equal((await bridge.handle(request({ ...attach, mode: "copy" })))[0], 400);

    // Concurrent duplicate requests create one attachment; later ones see the Markdown and skip.
    const results = await Promise.all([bridge.handle(request(attach)), bridge.handle(request(attach))]);
    assert(results.every(r => r[0] === 200));
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "import");
    assert.equal(calls[0][1].contentType, "text/markdown");
    assert.equal(body(results[0]).skipped, false);
    assert.equal(body(results[1]).skipped, true);
    assert.equal(body(results[0]).key, body(results[1]).key);

    // A paper that already has any Markdown attachment never gets a second one.
    result = await bridge.handle(request({ ...attach, file: linked, mode: "link" }));
    assert.equal(body(result).skipped, true);
    assert.equal(calls.length, 1);

    // An AI summary card is attached beside the Markdown (it is not "a second Markdown"), with an optional note.
    const summaryRequest = { ...attach, kind: "summary", file: summary, title: "SUM_Liu 等_Title", note_html: "<h1>卡片</h1>" };
    result = await bridge.handle(request(summaryRequest));
    assert.equal(body(result).skipped, false);
    assert.equal(body(result).note_key, "NOTE0001");
    assert.equal(notes.length, 1);
    assert.equal(notes[0].parentID, parent.id);
    assert.equal(notes[0].html, "<h1>卡片</h1>");
    result = await bridge.handle(request(summaryRequest));
    assert.equal(body(result).skipped, true, "a second summary card is never added");
    assert.equal(notes.length, 1);
    assert.equal((await bridge.handle(request({ ...summaryRequest, kind: "other" })))[0], 400);

    // Bilingual PDFs: only 双语_/译文_ PDF files, stored as PDFs, one of each per paper.
    const translation = { ...attach, kind: "translation", file: bilingual, title: "双语_Liu 等_Title" };
    assert.equal((await bridge.handle(request({ ...translation, file: disguised })))[0], 400);
    assert.equal((await bridge.handle(request({ ...translation, file: markdown })))[0], 400);
    result = await bridge.handle(request(translation));
    assert.equal(body(result).skipped, false);
    const pdfCall = calls[calls.length - 1];
    assert.equal(pdfCall[1].contentType, "application/pdf");
    assert.equal(pdfCall[1].charset, undefined);
    result = await bridge.handle(request({ ...translation, file: mono, title: "译文_Liu 等_Title" }));
    assert.equal(body(result).skipped, false, "the translation-only PDF is not a second bilingual PDF");
    result = await bridge.handle(request(translation));
    assert.equal(body(result).skipped, true, "a second bilingual PDF is never added");
    result = await bridge.handle(request({ ...attach }));
    assert.equal(body(result).skipped, true, "a bilingual PDF does not count as the paper's Markdown, which already exists");

    // Upgrading replaces the old folder-based result and moves it to the trash.
    result = await bridge.handle(request({ ...attach, parent_key: "PARENT02", file: path.join(work, "PARENT02", "MD_x.md"), replace_key: "LEGACY02" }));
    assert.equal(result[0], 200);
    assert.equal(body(result).replaced, "LEGACY02");
    assert.equal(legacy.deleted, true);
    assert.equal(legacy.saved, 1);

    // Trash: only a paper's own attachments/notes, never the paper or another item's files.
    const pdfOfFirst = [...items.values()].find(i => i.key === "PDFKEY01");
    assert.equal((await bridge.handle(request({ op: "trash", parent_key: "PARENT02", keys: ["PDFKEY01"] })))[0], 400);
    assert.equal(pdfOfFirst.deleted, false);
    assert.equal((await bridge.handle(request({ op: "trash", parent_key: "PARENT01", keys: ["PARENT01"] })))[0], 400);
    assert.equal((await bridge.handle(request({ op: "trash", parent_key: "PARENT01", keys: ["bad"] })))[0], 400);
    result = await bridge.handle(request({ op: "trash", parent_key: "PARENT01", keys: ["PDFKEY01", "MISSING9"] }));
    assert.equal(result[0], 200);
    assert.deepEqual(body(result).trashed, ["PDFKEY01"]);
    assert.equal(pdfOfFirst.deleted, true);
    assert.deepEqual(trashedIds, [pdfOfFirst.id], "uses Zotero's own move-to-trash");

    bridge.stop();
    assert.equal(context.Zotero.Server.Endpoints["/mineru-bridge/v1"], undefined);
    console.log("Bridge checks passed: auth, browser rejection, fixed operations, Zotero names, folder scope, skip existing Markdown, summary card + note, bilingual PDF kind, concurrent dedup, legacy replace, trash scope, lifecycle.");
})().catch(error => { console.error(error); process.exitCode = 1; });
