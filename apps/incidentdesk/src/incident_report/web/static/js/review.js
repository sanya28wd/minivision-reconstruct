/* Citation clocks remain per view: comparing never invents a cross-camera offset. */
"use strict";
(() => {
  const root = document.querySelector("[data-review-workspace]");
  if (!root) return;
  const panel = root.querySelector(".review-player");
  const primary = root.querySelector("#review-primary");
  const secondary = root.querySelector("#review-secondary");
  const compare = root.querySelector("#review-compare");
  const status = root.querySelector("#review-status");
  const videos = [...root.querySelectorAll("video[data-view-id]")];
  const events = [...root.querySelectorAll("[data-timeline-event]")];
  const findVideo = id => videos.find(video => video.dataset.viewId === id);
  const visible = () => videos.filter(video => !video.closest("figure").hidden);
  function updateViews() {
    if (compare.checked && secondary.value === primary.value) {
      const alternative = videos.find(video => video.dataset.viewId !== primary.value);
      if (alternative) secondary.value = alternative.dataset.viewId;
    }
    root.querySelector("#review-secondary-controls").hidden = !compare.checked;
    root.querySelector(".review-videos").classList.toggle("is-comparing", compare.checked);
    videos.forEach(video => {
      const shown = video.dataset.viewId === primary.value || (compare.checked && video.dataset.viewId === secondary.value);
      video.closest("figure").hidden = !shown;
      if (!shown) video.pause();
    });
  }
  function seek(id, seconds) {
    const video = findVideo(id);
    if (!video || !Number.isFinite(seconds) || seconds < 0) {
      status.textContent = "This citation has no playable view.";
      return;
    }
    if (video.closest("figure").hidden) { primary.value = id; updateViews(); }
    const apply = () => {
      if (seconds > video.duration) {
        status.textContent = "This citation is outside the playable video.";
        return;
      }
      video.currentTime = seconds;
      status.textContent = `Selected view at ${seconds.toFixed(1)} seconds.`;
    };
    if (video.readyState >= 1) apply();
    else video.addEventListener("loadedmetadata", apply, { once: true });
  }
  root.addEventListener("click", event => {
    const button = event.target.closest("button[data-seconds]");
    if (button) seek(button.dataset.viewId, Number(button.dataset.seconds));
  });
  function togglePlayback() {
    const playing = visible().some(video => !video.paused);
    visible().forEach(video => {
      if (playing) video.pause();
      else video.play().catch(() => { status.textContent = "Video could not play. Check its format or availability."; });
    });
  }
  root.querySelector("#review-play").addEventListener("click", togglePlayback);
  [primary, secondary, compare].forEach(control => control.addEventListener("change", updateViews));
  panel.tabIndex = 0;
  panel.setAttribute("aria-describedby", "review-help");
  panel.addEventListener("keydown", event => {
    if (event.target !== panel) return; // Keep native video, form and button keyboard behaviour.
    if (event.code === "Space") { event.preventDefault(); togglePlayback(); }
    if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
      const video = findVideo(primary.value);
      const delta = event.key === "ArrowLeft" ? -1 : 1;
      if (video && Number.isFinite(video.duration)) seek(primary.value, Math.max(0, Math.min(video.duration, video.currentTime + delta)));
    }
  });
  videos.forEach(video => {
    const matching = events.filter(item => item.dataset.viewId === video.dataset.viewId);
    const track = [...root.querySelectorAll("[data-marker-view]")].find(item => item.dataset.markerView === video.dataset.viewId);
    function markers() {
      track.replaceChildren();
      if (!Number.isFinite(video.duration) || video.duration <= 0) return;
      matching.forEach(item => {
        const seconds = Number(item.dataset.seconds);
        if (!Number.isFinite(seconds) || seconds < 0 || seconds > video.duration) return;
        const marker = document.createElement("button");
        marker.type = "button";
        marker.className = "review-marker";
        marker.dataset.viewId = video.dataset.viewId;
        marker.dataset.seconds = item.dataset.seconds;
        marker.textContent = item.dataset.kind === "collision" ? "C" : "E";
        marker.title = `${item.dataset.kind} at ${seconds.toFixed(1)}s`;
        marker.setAttribute("aria-label", marker.title);
        // CSSOM positioning, no inline script or model-generated markup under CSP.
        marker.style.left = `${seconds / video.duration * 100}%`;
        track.append(marker);
      });
    }
    video.addEventListener("loadedmetadata", markers);
    video.addEventListener("durationchange", markers);
    video.addEventListener("error", () => { status.textContent = "Video unavailable or unsupported. The cited report remains below."; });
    video.addEventListener("timeupdate", () => {
      // Current event means the latest cited moment reached on this view's clock.
      const reached = matching.filter(item => Number(item.dataset.seconds) <= video.currentTime);
      const latest = Math.max(...reached.map(item => Number(item.dataset.seconds)));
      matching.forEach(item => {
        if (Number(item.dataset.seconds) === latest) item.setAttribute("aria-current", "true");
        else item.removeAttribute("aria-current");
      });
    });
    markers();
  });
  updateViews();
})();
