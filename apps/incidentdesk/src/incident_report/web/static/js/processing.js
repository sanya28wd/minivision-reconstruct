"use strict";

(function () {
  var container = document.querySelector(".processing-card");
  if (!container) return;

  var runId = container.getAttribute("data-run-id");
  if (!runId) return;

  var stepOrder = [
    "uploading",
    "fingerprinting",
    "overview",
    "agreement",
    "collision check",
    "checks",
    "rules",
    "done"
  ];

  var startedAtMs = 0;
  var timerInterval = null;

  function updateElapsed(sec) {
    var el = document.getElementById("elapsed-time-val");
    if (el) {
      el.textContent = sec + "s";
    }
  }

  function startTimer(isoString) {
    if (!isoString) return;
    var parsed = new Date(isoString).getTime();
    if (isNaN(parsed)) return;
    startedAtMs = parsed;
    if (!timerInterval) {
      timerInterval = setInterval(function () {
        if (startedAtMs > 0) {
          var elapsed = Math.max(0, (Date.now() - startedAtMs) / 1000).toFixed(1);
          updateElapsed(elapsed);
        }
      }, 250);
    }
  }

  function updateSteps(currentStep) {
    var currentIdx = stepOrder.indexOf(currentStep);
    if (currentIdx === -1) return;

    var items = container.querySelectorAll(".step-item");
    for (var i = 0; i < items.length; i++) {
      var li = items[i];
      var key = li.getAttribute("data-step");
      var idx = stepOrder.indexOf(key);
      if (idx === -1) continue;

      var labelEl = li.querySelector(".step-label");
      var labelText = labelEl ? (labelEl.textContent || "").trim() : key;

      var marker = document.createElement("span");
      marker.className = "step-marker";
      marker.setAttribute("aria-hidden", "true");
      var label = document.createElement("span");
      label.className = "step-label";
      label.textContent = labelText;
      li.removeAttribute("aria-current");

      if (idx < currentIdx) {
        li.className = "step-item step-item--done";
        marker.textContent = "[x]";
      } else if (idx === currentIdx) {
        li.className = "step-item step-item--active";
        li.setAttribute("aria-current", "step");
        marker.textContent = "[>]";
        var strong = document.createElement("strong");
        strong.textContent = labelText;
        label.replaceChildren(strong);
      } else {
        li.className = "step-item step-item--pending";
        marker.textContent = "[ ]";
      }
      li.replaceChildren(marker, document.createTextNode(" "), label);
      if (idx === currentIdx) {
        var badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "In progress";
        li.appendChild(document.createTextNode(" "));
        li.appendChild(badge);
      }
    }
  }

  var source = new EventSource("/reports/" + runId + "/events");

  source.addEventListener("step", function (event) {
    try {
      var data = JSON.parse(event.data);
      if (data.step) {
        updateSteps(data.step);
      }
      if (data.started_at) {
        startTimer(data.started_at);
      }
      if (data.elapsed_seconds !== undefined) {
        updateElapsed(data.elapsed_seconds);
      }
    } catch (e) {}
  });

  source.addEventListener("done", function (event) {
    source.close();
    if (timerInterval) clearInterval(timerInterval);
    try {
      var data = JSON.parse(event.data);
      window.location.href = data.redirect || ("/reports/" + runId);
    } catch (e) {
      window.location.href = "/reports/" + runId;
    }
  });

  var streamReloadPending = false;

  source.addEventListener("error", function (event) {
    // If the server sent an explicit error event with data, reload to show failed screen with Retry
    if (event.data) {
      source.close();
      if (timerInterval) clearInterval(timerInterval);
      window.location.reload();
    } else if (source.readyState === EventSource.CLOSED && !streamReloadPending) {
      streamReloadPending = true;
      source.close();
      if (timerInterval) clearInterval(timerInterval);
      var message = document.createElement("p");
      message.setAttribute("role", "status");
      message.textContent = "Lost connection to the progress stream. Reloading…";
      container.appendChild(message);
      setTimeout(function () {
        window.location.reload();
      }, 3000);
    }
  });
})();
