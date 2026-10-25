/*
 * Barcode scanning and photos (Add food's Scan and Describe tabs, the recipe builder).
 *
 * Scanning uses the browser's built-in BarcodeDetector (Chrome on Android), over a
 * full-screen camera view drawn outside Dash. A code is accepted once it reads the
 * same twice in a row, then written to a store: `scan-code` unless the caller names
 * another with open({code, msg}). Problems go to the `msg` text (`scan-js-msg`).
 * Photos are downscaled here (Claude reads at most ~1568 px) and written to a store:
 * `label-photo` (taken with the camera), unless pickPhoto({store, msg, camera})
 * names another. Plate photos pass camera: false so one taken earlier can be picked.
 */
(function () {
  "use strict";

  const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e"];
  const SCAN_INTERVAL_MS = 120;
  const PHOTO_MAX_EDGE = 1568;

  const DEFAULT_TARGET = { code: "scan-code", msg: "scan-js-msg" };
  const DEFAULT_PHOTO = { store: "label-photo", msg: "scan-js-msg", camera: true };

  let session = null; // {overlay, stream, track, running, target}
  let target = DEFAULT_TARGET;
  let photoInput = null;
  let photoTarget = DEFAULT_PHOTO;

  function setProps(id, props) {
    if (window.dash_clientside && window.dash_clientside.set_props) {
      window.dash_clientside.set_props(id, props);
    }
  }

  function message(text) {
    setProps(target.msg, { children: text || "" });
  }

  function cameraError(err) {
    if (!window.isSecureContext) {
      return "The camera only works over HTTPS. Open the app through its Tailscale https:// address.";
    }
    switch (err && err.name) {
      case "NotAllowedError":
        return "Camera access was blocked. Allow it for this site in Chrome (lock icon › Permissions), then try again.";
      case "NotFoundError":
      case "OverconstrainedError":
        return "No camera was found on this device.";
      case "NotReadableError":
        return "The camera is busy in another app. Close it and try again.";
      default:
        return "Couldn't start the camera" + (err && err.message ? ": " + err.message : ".");
    }
  }

  async function makeDetector() {
    if (!("BarcodeDetector" in window)) {
      return null;
    }
    let supported = FORMATS;
    try {
      supported = await window.BarcodeDetector.getSupportedFormats();
    } catch (e) {
      /* older builds: assume the product formats */
    }
    const formats = FORMATS.filter((f) => supported.includes(f));
    return formats.length ? new window.BarcodeDetector({ formats }) : null;
  }

  function buildOverlay() {
    const overlay = document.createElement("div");
    overlay.className = "gwc-scan-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-label", "Scan a barcode");
    overlay.innerHTML =
      '<video class="gwc-scan-video" autoplay muted playsinline></video>' +
      '<div class="gwc-scan-frame" aria-hidden="true"></div>' +
      '<p class="gwc-scan-hint">Line up the barcode inside the box</p>' +
      '<div class="gwc-scan-bar">' +
      '<button type="button" class="gwc-scan-btn" data-act="torch" hidden>Light</button>' +
      '<button type="button" class="gwc-scan-btn" data-act="close">Cancel</button>' +
      "</div>";
    overlay.addEventListener("click", (e) => {
      const act = e.target && e.target.getAttribute("data-act");
      if (act === "close") close();
      if (act === "torch") toggleTorch(e.target);
    });
    document.body.appendChild(overlay);
    return overlay;
  }

  async function toggleTorch(button) {
    if (!session || !session.track) return;
    session.torch = !session.torch;
    try {
      await session.track.applyConstraints({ advanced: [{ torch: session.torch }] });
      button.classList.toggle("is-on", session.torch);
    } catch (e) {
      button.hidden = true;
    }
  }

  function onKey(e) {
    if (e.key !== "Escape") return;
    e.stopPropagation(); // don't also close a dialog the scanner was opened from
    close();
  }

  function onHide() {
    if (document.hidden) close();
  }

  function close() {
    if (!session) return;
    session.running = false;
    if (session.stream) session.stream.getTracks().forEach((t) => t.stop());
    session.overlay.remove();
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("visibilitychange", onHide);
    session = null;
  }

  async function open(where) {
    if (session) return;
    target = Object.assign({}, DEFAULT_TARGET, where || {});
    message("");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      message(cameraError(null));
      return;
    }
    const detector = await makeDetector();
    if (!detector) {
      message(
        "This browser can't read barcodes (Chrome on Android can). Type the number under the bars instead."
      );
      return;
    }
    session = { overlay: buildOverlay(), stream: null, track: null, running: true, torch: false };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("visibilitychange", onHide);
    const current = session;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          facingMode: { ideal: "environment" },
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
      });
    } catch (err) {
      close();
      message(cameraError(err));
      return;
    }
    if (session !== current) {
      stream.getTracks().forEach((t) => t.stop()); // cancelled while asking
      return;
    }
    session.stream = stream;
    session.track = stream.getVideoTracks()[0];
    const caps = (session.track.getCapabilities && session.track.getCapabilities()) || {};
    if (caps.torch) {
      session.overlay.querySelector('[data-act="torch"]').hidden = false;
    }
    if (caps.focusMode && caps.focusMode.includes("continuous")) {
      session.track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
    }
    const video = session.overlay.querySelector("video");
    video.srcObject = stream;
    try {
      await video.play();
    } catch (e) {
      /* autoplay is allowed for muted video; ignore */
    }
    scanLoop(current, detector, video);
  }

  function scanLoop(current, detector, video) {
    let last = null;
    async function tick() {
      if (!current.running) return;
      if (video.readyState >= 2) {
        let codes = [];
        try {
          codes = await detector.detect(video);
        } catch (e) {
          /* frame not ready */
        }
        const code = codes.find((c) => FORMATS.includes(c.format));
        if (code) {
          if (last && last.rawValue === code.rawValue) {
            found(code);
            return;
          }
          last = code;
        } else {
          last = null;
        }
      }
      setTimeout(tick, SCAN_INTERVAL_MS);
    }
    tick();
  }

  function found(code) {
    if (navigator.vibrate) navigator.vibrate(60);
    close();
    setProps(target.code, {
      data: { code: code.rawValue, format: code.format, nonce: Date.now() },
    });
  }

  // ------------------------------------------------------------------ photos

  async function downscale(file) {
    let source;
    try {
      source = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch (e) {
      source = await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = URL.createObjectURL(file);
      });
    }
    const w = source.width;
    const h = source.height;
    const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(w, h));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.88);
  }

  function pickPhoto(opts) {
    photoTarget = Object.assign({}, DEFAULT_PHOTO, opts || {});
    setProps(photoTarget.msg, { children: "" });
    if (!photoInput) {
      photoInput = document.createElement("input");
      photoInput.type = "file";
      photoInput.accept = "image/*";
      photoInput.hidden = true;
      photoInput.addEventListener("change", async () => {
        const file = photoInput.files && photoInput.files[0];
        photoInput.value = "";
        if (!file) return;
        try {
          const image = await downscale(file);
          setProps(photoTarget.store, { data: { image, nonce: Date.now() } });
        } catch (e) {
          setProps(photoTarget.msg, { children: "Couldn't open that photo. Try taking it again." });
        }
      });
      document.body.appendChild(photoInput);
    }
    if (photoTarget.camera) {
      photoInput.setAttribute("capture", "environment");
    } else {
      photoInput.removeAttribute("capture");
    }
    photoInput.click();
  }

  window.gwcScanner = { open, close, pickPhoto };
})();
