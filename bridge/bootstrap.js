var MineruBridge;

function install() {}
function uninstall() {}

async function startup({ rootURI }) {
    await Zotero.initializationPromise;
    Services.scriptloader.loadSubScript(rootURI + "bridge.js");
    MineruBridge.start();
}

function shutdown() {
    if (MineruBridge) MineruBridge.stop();
    MineruBridge = undefined;
}
