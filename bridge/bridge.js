/* Fixed operations only: status, Zotero-style file names, validated Markdown/summary/bilingual-PDF attachment
   creation, and moving a paper's own attachments/notes to the Zotero trash. */
var MineruBridge = {
    PROTOCOL: 5,
    VERSION: "2.3.1",
    // Same pieces Zotero uses for PDF names ("作者 - 年份 - 标题"), joined as 作者_标题.
    NAME_TEMPLATE: '{{ firstCreator suffix="_" }}{{ title truncate="100" }}',
    revision: 0,
    boot: String(Date.now()) + "-" + Math.random().toString(36).slice(2),
    observer: null,
    writes: Promise.resolve(),

    start() {
        const state = this;
        this.observer = Zotero.Notifier.registerObserver({
            notify() { state.revision++; }
        }, ["item", "collection", "collection-item", "file"], "mineru-bridge");
        Zotero.Server.Endpoints["/mineru-bridge/v1"] = class {
            supportedMethods = ["POST"];
            supportedDataTypes = ["application/json"];
            permitBookmarklet = false;
            async init(request) {
                return state.handle(request);
            }
        };
        Zotero.debug("MinerU Markdown bridge " + this.VERSION + " started");
    },

    stop() {
        if (this.observer) Zotero.Notifier.unregisterObserver(this.observer);
        delete Zotero.Server.Endpoints["/mineru-bridge/v1"];
    },

    async pairing() {
        const override = Services.env.get("MINERU_ZOTERO_HOME");
        const home = override || PathUtils.join(Services.env.get("LOCALAPPDATA"), "ZoteroMinerU");
        return IOUtils.readJSON(PathUtils.join(home, "bridge.json"));
    },

    reply(status, data) {
        return [status, "application/json", JSON.stringify(data)];
    },

    async handle(request) {
        try {
            const header = name => typeof request.headers.get === "function"
                ? request.headers.get(name) : request.headers[name.toLowerCase()];
            if (header("Origin")) return this.reply(403, { ok: false, error: "不接受网页发起的桥接请求。" });
            let pairing;
            try { pairing = await this.pairing(); }
            catch (_) { return this.reply(403, { ok: false, error: "桥接配对认证失败：请先打开转换软件并保存设置。" }); }
            const supplied = header("X-MinerU-Bridge-Token") || "";
            const expected = pairing.token || "";
            let difference = supplied.length ^ expected.length;
            for (let i = 0; i < expected.length; i++) difference |= expected.charCodeAt(i) ^ (supplied.charCodeAt(i) || 0);
            if (expected.length < 32 || difference) return this.reply(403, { ok: false, error: "桥接配对认证失败。" });
            const data = request.data || {};
            if (data.op === "status") {
                return this.reply(200, { ok: true, protocol: this.PROTOCOL, version: this.VERSION, revision: this.revision,
                    boot: this.boot, data_directory: Zotero.DataDirectory.dir, zotero_version: Zotero.version });
            }
            if (data.op === "names") {
                if (!Array.isArray(data.keys) || data.keys.length > 500) throw new Error("请求条目数量无效。");
                const names = {};
                for (const key of data.keys) {
                    if (!/^[A-Z0-9]{8}$/.test(key)) throw new Error("条目标识无效。");
                    const item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, key);
                    if (!item || !item.isRegularItem()) continue;
                    try {
                        names[key] = Zotero.Attachments.getFileBaseNameFromItem(item, { formatString: this.NAME_TEMPLATE });
                    }
                    catch (error) { Zotero.logError(error); }
                }
                return this.reply(200, { ok: true, names });
            }
            if (data.op === "attach") {
                const pending = this.writes.then(() => this.attach(data, pairing));
                this.writes = pending.catch(() => {});
                return this.reply(200, { ok: true, ...(await pending) });
            }
            if (data.op === "trash") {
                const pending = this.writes.then(() => this.trash(data));
                this.writes = pending.catch(() => {});
                return this.reply(200, { ok: true, ...(await pending) });
            }
            return this.reply(400, { ok: false, error: "不支持的桥接操作。" });
        }
        catch (error) {
            Zotero.logError(error);
            return this.reply(400, { ok: false, error: String(error.message || error).slice(0, 400) });
        }
    },

    normalized(path) {
        const file = Zotero.File.pathToFile(path);
        file.normalize();
        return file;
    },

    isMarkdownFile(item) {
        const type = item.attachmentContentType || "";
        const name = item.attachmentFilename || "";
        return /markdown/i.test(type) || /\.(md|markdown)$/i.test(name);
    },

    // AI summary cards (SUM_作者_论文名称.md) are not the paper's full-text Markdown.
    isSummary(item) {
        return this.isMarkdownFile(item) && /^SUM_/i.test(item.attachmentFilename || "");
    },

    isMarkdown(item) {
        return this.isMarkdownFile(item) && !this.isSummary(item);
    },

    // Translated PDFs made by the converter: 双语_作者_论文名称.pdf (side by side) and 译文_作者_论文名称.pdf.
    isTranslation(item, prefix) {
        return item.attachmentContentType === "application/pdf" && (item.attachmentFilename || "").startsWith(prefix);
    },

    async attach(data, pairing) {
        const kind = data.kind || "markdown";
        if (!/^[A-Z0-9]{8}$/.test(data.parent_key) || (data.replace_key && !/^[A-Z0-9]{8}$/.test(data.replace_key))
                || !["import", "link"].includes(data.mode) || !["markdown", "summary", "translation"].includes(kind)) {
            throw new Error("附件参数无效。");
        }
        const libraryID = Zotero.Libraries.userLibraryID;
        if (!Zotero.Libraries.get(libraryID).editable) throw new Error("当前文库不可写。");
        const parent = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, data.parent_key);
        if (!parent || parent.deleted || !parent.isRegularItem()) throw new Error("论文条目不存在或已删除。");

        const file = this.normalized(data.file);
        const pdf = kind === "translation";
        const pattern = pdf ? /^(双语|译文)_.+\.pdf$/i : /\.md$/i;
        if (!file.exists() || !file.isFile() || !pattern.test(file.leafName)) {
            throw new Error(pdf ? "双语 PDF 文件不存在。" : "Markdown 文件不存在。");
        }
        const lower = file.path.toLowerCase();
        const separator = Zotero.isWin ? "\\" : "/";
        const permitted = (pairing.allowed_roots || []).some(rootPath => {
            const root = this.normalized(rootPath).path.replace(/[\\/]+$/, "");
            return lower.startsWith(root.toLowerCase() + separator) && !file.path.slice(root.length).split(/[\\/]+/).includes("..");
        });
        if (!permitted) throw new Error("文件不在转换软件的工作目录或输出目录中。");

        let replaced = null;
        const prefix = file.leafName.slice(0, 3);
        const sameKind = item => (kind === "summary" ? this.isSummary(item)
            : pdf ? this.isTranslation(item, prefix) : this.isMarkdown(item));
        for (const id of parent.getAttachments(false)) {
            const existing = await Zotero.Items.getAsync(id);
            if (!existing || existing.deleted || !sameKind(existing)) continue;
            const path = await existing.getFilePathAsync();
            if (path && this.normalized(path).path.toLowerCase() === lower) {
                return { key: existing.key, path, skipped: true };
            }
            if (data.replace_key && existing.key === data.replace_key) { replaced = existing; continue; }
            // A paper that already has a Markdown (or a summary card, or a bilingual PDF) is never given a second one.
            if (!data.replace_key) return { key: existing.key, path: path || "", skipped: true };
        }

        const options = { file: file.path, parentItemID: parent.id, title: String(data.title || file.leafName).slice(0, 240),
            contentType: pdf ? "application/pdf" : "text/markdown" };
        if (!pdf) options.charset = "utf-8";
        const item = data.mode === "link"
            ? await Zotero.Attachments.linkFromFile(options)
            : await Zotero.Attachments.importFromFile(options);
        if (replaced) {
            replaced.deleted = true;
            await replaced.saveTx();
        }
        let noteKey = "";
        if (kind === "summary" && data.note_html) {
            const note = new Zotero.Item("note");
            note.libraryID = libraryID;
            note.parentID = parent.id;
            note.setNote(String(data.note_html).slice(0, 500000));
            await note.saveTx();
            noteKey = note.key;
        }
        return { key: item.key, path: (await item.getFilePathAsync()) || file.path, skipped: false,
            replaced: replaced ? replaced.key : "", note_key: noteKey };
    },

    // Moves attachments/notes of one paper to the Zotero trash (recoverable there). Never touches the paper itself
    // or anything belonging to another item, and never erases files.
    async trash(data) {
        if (!/^[A-Z0-9]{8}$/.test(data.parent_key) || !Array.isArray(data.keys) || !data.keys.length || data.keys.length > 50
                || data.keys.some(key => !/^[A-Z0-9]{8}$/.test(key))) {
            throw new Error("删除参数无效。");
        }
        const libraryID = Zotero.Libraries.userLibraryID;
        if (!Zotero.Libraries.get(libraryID).editable) throw new Error("当前文库不可写。");
        const parent = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, data.parent_key);
        if (!parent || !parent.isRegularItem()) throw new Error("论文条目不存在。");
        const items = [];
        for (const key of data.keys) {
            const item = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, key);
            if (!item || item.deleted) continue;
            if (item.parentID !== parent.id || !(item.isAttachment() || item.isNote())) {
                throw new Error("只能删除该论文条目下的附件或笔记。");
            }
            items.push(item);
        }
        if (items.length) {
            if (typeof Zotero.Items.trashTx === "function") {
                await Zotero.Items.trashTx(items.map(item => item.id));
            }
            else {
                for (const item of items) {
                    item.deleted = true;
                    await item.saveTx();
                }
            }
        }
        return { trashed: items.map(item => item.key) };
    }
};
