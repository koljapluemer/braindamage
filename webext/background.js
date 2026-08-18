// Background page: the only piece of this extension allowed to talk to the
// native host (browser.runtime.sendNativeMessage isn't available to content
// scripts, e.g. sidebar.js -- only to background/extension pages), so it
// exists purely as a relay, plus the toolbar-icon toggle for the sidebar.

const NATIVE_HOST = "braindamage_steam_offers";

const RELAYED_MESSAGE_TYPES = ["fetchOffers", "constructContract"];

browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !RELAYED_MESSAGE_TYPES.includes(message.type)) return undefined;

  browser.runtime
    .sendNativeMessage(NATIVE_HOST, message.payload)
    .then((reply) => sendResponse({ ok: true, reply }))
    .catch((err) => sendResponse({ ok: false, error: err && err.message ? err.message : String(err) }));
  return true; // keep the message channel open for the async sendResponse above
});

browser.action.onClicked.addListener(async (tab) => {
  try {
    await browser.tabs.sendMessage(tab.id, { type: "toggleSidebar" });
  } catch (e) {
    // No sidebar content script on this tab (not a Steam Market listing
    // page) -- nothing to toggle, nothing to do.
  }
});
