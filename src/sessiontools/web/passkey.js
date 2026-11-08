// The front door's passkey ceremonies, the browser's half (PROPOSED,
// door-passkey). Served at /login/passkey.js only when the `passkey` extra
// is installed; a file, not inline, because the CSP is default-src 'self'.
// #passkey-signin (the login page) signs in with a passkey kept for this
// name; #passkey-enrol (the menu, just after a pairing-code login) adds one.
// Each POST is same-origin, so the browser sends the Origin the door checks.
"use strict";
(function () {
  function b64u(buf) {
    var s = "", b = new Uint8Array(buf);
    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function unb64u(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var b = atob(s), out = new Uint8Array(b.length);
    for (var i = 0; i < b.length; i++) out[i] = b.charCodeAt(i);
    return out.buffer;
  }
  function ids(list) {
    return list.map(function (id) { return { type: "public-key", id: unb64u(id) }; });
  }
  function post(path, body) {
    return fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("the front door said " + r.status));
        return j;
      });
    });
  }
  function say(text) {
    var el = document.getElementById("passkey-said");
    if (el) el.textContent = text;
  }
  function why(e) {
    if (e && e.name === "NotAllowedError") return "Passkey: cancelled, or it took too long.";
    return "Passkey: " + ((e && e.message) || (e && e.name) || "it did not work") + ".";
  }
  function device() {
    var ua = navigator.userAgent;
    if (/iPhone/.test(ua)) return "iPhone";
    if (/iPad/.test(ua)) return "iPad";
    if (/Android/.test(ua)) return "Android";
    if (/Macintosh/.test(ua)) return "Mac";
    return "a browser";
  }

  var signin = document.getElementById("passkey-signin");
  if (signin) signin.addEventListener("click", function () {
    say("");
    post("/login/passkey/begin").then(function (o) {
      return navigator.credentials.get({ publicKey: {
        challenge: unb64u(o.challenge), rpId: o.rpId, userVerification: "required",
        allowCredentials: ids(o.allow), timeout: 60000
      } }).then(function (a) {
        return post("/login/passkey/finish", {
          challenge: o.challenge, id: a.id,
          clientDataJSON: b64u(a.response.clientDataJSON),
          authenticatorData: b64u(a.response.authenticatorData),
          signature: b64u(a.response.signature)
        });
      });
    }).then(function (j) { location.assign(j.to || "/"); }, function (e) { say(why(e)); });
  });

  var enrol = document.getElementById("passkey-enrol");
  if (enrol) enrol.addEventListener("click", function () {
    say("");
    post("/passkey/enrol/begin").then(function (o) {
      return navigator.credentials.create({ publicKey: {
        challenge: unb64u(o.challenge), rp: { id: o.rpId, name: o.rpName },
        user: { id: unb64u(o.user.id), name: o.user.name, displayName: o.user.displayName },
        pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
        authenticatorSelection: { userVerification: "required", residentKey: "preferred" },
        excludeCredentials: ids(o.exclude), attestation: "none", timeout: 60000
      } }).then(function (c) {
        return post("/passkey/enrol/finish", {
          challenge: o.challenge, id: c.id, name: device(),
          clientDataJSON: b64u(c.response.clientDataJSON),
          authenticatorData: b64u(c.response.getAuthenticatorData()),
          publicKey: b64u(c.response.getPublicKey()),
          alg: c.response.getPublicKeyAlgorithm()
        });
      });
    }).then(function () {
      enrol.hidden = true;
      say("Passkey added. Next time, sign in with it.");
    }, function (e) { say(why(e)); });
  });
})();
