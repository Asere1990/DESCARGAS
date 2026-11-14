"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "web_galerias", "manual_upload.js"), "utf8");

function mounted(files) {
  const nodes = {};
  function node(id) {
    if (!nodes[id]) {
      const flags = new Set(["hidden"]);
      nodes[id] = {
        id, value: "", textContent: "", disabled: false,
        classList: {
          contains: flag => flags.has(flag),
          add: flag => flags.add(flag),
          remove: flag => flags.delete(flag),
        },
        addEventListener(name, listener) { this[name] = listener; },
        scrollIntoView() { this.scrolled = true; },
        replaceChildren() {}, add() {},
      };
    }
    return nodes[id];
  }
  const captured = [];
  const store = new Map();
  const win = {};
  const api = async (operation, data = {}) => {
    captured.push({operation, data});
    if (operation === "admin-manual-config") return {ready: true};
    if (operation === "admin-manual-start") {
      return {
        upload: data.filename === "uno.jpg"
          ? "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
          : "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb",
        status: "uploading",
        multipart: false, url: "https://example.r2/upload",
        thumb_url: "https://example.r2/thumb",
      };
    }
    if (operation === "admin-manual-complete") return {ok: true, media: 99};
    if (operation === "admin-manual-caption") return {ok: true, updated: data.uploads.length};
    throw new Error("Unexpected operation: " + operation);
  };
  const document = {
    getElementById: node,
    createElement(type) {
      if (type === "img") {
        return {
          naturalWidth: 640, naturalHeight: 480,
          set src(value) { queueMicrotask(() => this.onload?.()); }
        };
      }
      if (type === "canvas") {
        return {
          width: 0, height: 0,
          getContext() { return {fillRect() {}, drawImage() {}, fillStyle: ""}; },
          toBlob(callback) { callback({size: 100}); }
        };
      }
      throw Error("Unexpected element " + type);
    }
  };
  node("manual-r2-folder").value = "7";
  node("manual-r2-files").files = files;
  const context = {
    document, window: win, api, loadAdmin: async () => {},
    S: {me: {admin: true}},
    localStorage: {
      getItem: k => store.get(k) ?? null,
      setItem: (k, v) => store.set(k, v),
      removeItem: k => store.delete(k),
    },
    URL: {createObjectURL: () => "blob:photo", revokeObjectURL: () => {}},
    fetch: async () => ({ok: true}),
    Option: function Option() {},
    queueMicrotask,
    console,
    setTimeout, clearTimeout,
  };
  vm.runInNewContext(source, context, {filename: "manual_upload.js"});
  return {node, captured, window: win};
}

const photo = name => ({name, size: 300, lastModified: 1});

test("a finished upload automatically offers a caption and indexes its batch", async () => {
  const view = mounted([photo("uno.jpg")]);
  await view.node("manual-r2-start").click();
  assert.equal(view.node("manual-r2-caption-panel").classList.contains("hidden"), false);
  assert.match(view.node("manual-r2-caption-count").textContent, /1 archivo publicado/);
  assert.equal(view.node("manual-r2-start").disabled, true);
  view.node("manual-r2-caption-text").value = "La Habana";
  await view.node("manual-r2-caption-save").click();
  const writes = view.captured.filter(x => x.operation === "admin-manual-caption");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].data.caption, "La Habana");
  assert.deepEqual([...writes[0].data.uploads], ["aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"]);
  assert.equal(view.node("manual-r2-caption-panel").classList.contains("hidden"), true);
  assert.equal(view.node("manual-r2-start").disabled, false);
});

test("Sin caption skips indexing while keeping the uploaded media", async () => {
  const view = mounted([photo("uno.jpg")]);
  await view.node("manual-r2-start").click();
  assert.equal(view.node("manual-r2-caption-panel").classList.contains("hidden"), false);
  await view.node("manual-r2-caption-skip").click();
  assert.equal(view.captured.filter(x => x.operation === "admin-manual-caption").length, 0);
  assert.equal(view.node("manual-r2-caption-panel").classList.contains("hidden"), true);
  assert.equal(view.node("manual-r2-start").disabled, false);
});

test("failed uploads do not receive the caption of successful photos", async () => {
  const view = mounted([photo("uno.jpg"), photo("malware.exe")]);
  await view.node("manual-r2-start").click();
  assert.equal(view.node("manual-r2-caption-panel").classList.contains("hidden"), false);
  assert.match(view.node("manual-r2-caption-count").textContent, /1 archivo publicado/);
  view.node("manual-r2-caption-text").value = "Viva Cuba";
  await view.node("manual-r2-caption-save").click();
  const writes = view.captured.filter(x => x.operation === "admin-manual-caption");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].data.uploads.length, 1);
});

test("empty caption never silently modifies the published batch", async () => {
  const view = mounted([photo("uno.jpg")]);
  await view.node("manual-r2-start").click();
  view.node("manual-r2-caption-text").value = "  ";
  await view.node("manual-r2-caption-save").click();
  assert.equal(view.captured.filter(x => x.operation === "admin-manual-caption").length, 0);
  assert.equal(view.node("manual-r2-caption-panel").classList.contains("hidden"), false);
});
