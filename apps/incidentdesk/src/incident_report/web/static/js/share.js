"use strict";
(async () => {
  const fragment = window.location.hash.slice(1);
  // Remove the bearer from the current history entry before any network request.
  history.replaceState(null, "", "/share");
  const match = /^([0-9a-f-]{36})\.([A-Za-z0-9_-]{43})$/.exec(fragment);
  const status = document.getElementById("share-status");
  try {
    if (!match) throw new Error("invalid");
    const response = await fetch("/access/open", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({link_id: match[1], secret: match[2]})
    });
    if (!response.ok) throw new Error("refused");
    window.location.replace("/");
  } catch {
    status.textContent = "This share link could not be opened. Ask the owner for a current link, or try again later.";
  }
})();
