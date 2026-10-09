// Clip Hunter dashboard.
//
// The pages are rendered on the server and work as plain pages. This script
// makes them quick to use: it opens a moment from the queue without loading
// the page again, saves a rating, tag or note as it is set, drives the clip
// player, and runs the review from the keyboard (1 to 5 rate, J and K move,
// Space plays).
//
// Everything is wired up by delegation from the document, because the open
// moment's markup is replaced every time another one is opened.
(() => {
  "use strict";

  const one = (selector, root = document) => root.querySelector(selector);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const plural = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;

  // The response, or null when the service could not be reached at all.
  async function post(url, body) {
    const options = { method: "POST", keepalive: true };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    try {
      return await fetch(url, options);
    } catch {
      return null;
    }
  }

  // Says something in the status line of whatever part of the page `from`
  // belongs to. Failures are stated there in words; an empty string clears it.
  function say(from, words) {
    const scope = from.closest("[data-status-scope]");
    const line = scope && one("[data-status]", scope);
    if (line) line.textContent = words;
  }

  // ---- What is new ------------------------------------------------------

  // The page never reloads on its own - that kept interrupting whatever was
  // being watched or typed. It asks how many moments and clips there are
  // now and offers the difference in the queue instead.
  const NEWS_INTERVAL_MS = 30000;

  async function checkForNew() {
    const news = one("[data-news]");
    let status;
    try {
      const response = await fetch("/moments/status");
      if (!response.ok) return;
      status = await response.json();
    } catch {
      return; // restarting or down - keep whatever the banner says
    }
    const moments = status.moments - Number(news.dataset.moments);
    const clips = status.clips - Number(news.dataset.clips);
    const parts = [];
    if (moments > 0) parts.push(plural(moments, "new moment"));
    if (clips > 0) parts.push(plural(clips, "new clip"));
    news.hidden = parts.length === 0;
    one("[data-news-words]", news).textContent = `${parts.join(" and ")} since you opened this page`;
  }

  // ---- The queue --------------------------------------------------------

  const rows = () => all("a.ch-row");
  const currentRow = () => one('a.ch-row[aria-current="true"]');
  const isPlainClick = (event) =>
    event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;

  let opening = 0; // counts requests to open a moment, so only the latest one lands
  let loading = 0; // how many of them are still on their way

  // Opens the moment of a queue row beside the queue. The row is an ordinary
  // link to the same thing, which is what happens if this cannot be done.
  async function openMoment(row, { play = false } = {}) {
    const ticket = ++opening;
    loading += 1;
    let markup = null;
    try {
      await saveNote();
      const response = await fetch(`/dashboard/moments/${row.dataset.moment}`);
      if (response.ok) markup = await response.text();
    } catch {
      // handled below, like any other answer that is not the moment
    } finally {
      loading -= 1;
    }
    if (ticket !== opening) return;
    if (markup === null) {
      location.assign(row.href);
      return;
    }

    one("[data-open]").innerHTML = markup;
    for (const other of rows()) other.removeAttribute("aria-current");
    row.setAttribute("aria-current", "true");
    row.scrollIntoView({ block: "nearest" });
    history.replaceState(null, "", row.href);
    dress();
    if (play) one("[data-clip]")?.play().catch(() => {});
  }

  function move(step) {
    const list = rows();
    if (!list.length) return;
    const index = list.indexOf(currentRow());
    const row = index < 0 ? list[step > 0 ? 0 : list.length - 1] : list[index + step];
    if (row) openMoment(row, { play: true });
  }

  // The next row down that still wants a rating, or failing that the first
  // one above.
  function nextUnrated() {
    const list = rows();
    const index = list.indexOf(currentRow());
    const unrated = (row) => row.classList.contains("is-unrated");
    return list.slice(index + 1).find(unrated) || list.slice(0, Math.max(index, 0)).find(unrated) || null;
  }

  // ---- Rating, tags, the note ------------------------------------------

  // The counts on the queue's tabs are totals for the channel it is narrowed
  // to, so they follow a rating only when the moment is from that channel.
  function recount(article, before, rating) {
    const queue = one("[data-queue]");
    if (!queue || (queue.dataset.channel && queue.dataset.channel !== article.dataset.channel)) return;
    const best = Number(queue.dataset.bestFrom);
    const bump = (show, by) => {
      const count = one(`[data-show="${show}"] [data-count]`, queue);
      if (count && by) count.textContent = String(Math.max(0, Number(count.textContent) + by));
    };
    bump("unrated", Number(rating === 0) - Number(before === 0));
    bump("best", Number(rating >= best) - Number(before >= best));
  }

  // Sets the rating, or clears it when the chosen key is pressed again.
  // Returns the rating now stored (0 for none), or null if it was not saved.
  async function rate(article, value) {
    const before = Number(article.dataset.rating);
    const rating = before === value ? 0 : value;
    const response = await post(`/moments/${article.dataset.moment}/rating?value=${rating}`);
    if (!response || !response.ok) {
      say(article, "Could not save the rating. Try again.");
      return null;
    }
    say(article, "");
    article.dataset.rating = String(rating);

    const keys = one("[data-rating-keys]", article);
    keys.setAttribute("aria-label", `Rating, ${rating ? `${rating} of 5` : "not rated yet"}`);
    for (const key of all("[data-rate]", keys)) {
      const number = Number(key.dataset.rate);
      key.classList.toggle("is-lit", number <= rating);
      key.setAttribute("aria-pressed", String(number === rating));
    }

    const row = one(`a.ch-row[data-moment="${article.dataset.moment}"]`);
    if (row) {
      row.classList.toggle("is-unrated", rating === 0);
      const meter = one(".ch-meter", row);
      meter.setAttribute("aria-label", rating ? `rated ${rating} of 5` : "not rated yet");
      all("i", meter).forEach((segment, index) => segment.classList.toggle("is-lit", index < rating));
    }
    recount(article, before, rating);
    return rating;
  }

  // A number key rates the open moment and moves on to the next unrated one.
  async function rateFromKeyboard(value) {
    const article = one(".moment");
    if (!article || loading) return;
    const rating = await rate(article, value);
    if (!rating) return;
    const next = nextUnrated();
    if (next) openMoment(next, { play: true });
  }

  // One tag per group; choosing the lit one again clears it.
  async function setTag(button) {
    const article = button.closest(".moment");
    const field = button.dataset.tag;
    const clearing = button.getAttribute("aria-pressed") === "true";
    const value = clearing ? "" : button.dataset.value;
    const response = await post(`/moments/${article.dataset.moment}/${field}?value=${encodeURIComponent(value)}`);
    if (!response || !response.ok) {
      say(article, "Could not save the tag. Try again.");
      return;
    }
    say(article, "");
    for (const other of all(`[data-tag="${field}"]`, article)) {
      other.setAttribute("aria-pressed", String(other === button && !clearing));
    }
  }

  // Saves the note if it was changed since it was last saved. Called when
  // the field is left, and before the moment it belongs to is swapped out.
  async function saveNote(field = one("[data-note]")) {
    if (!field || field.value === field.defaultValue) return;
    const article = field.closest(".moment");
    const saved = field.value;
    const response = await post(`/moments/${article.dataset.moment}/notes`, { notes: saved });
    if (!response || !response.ok) {
      say(article, "Could not save the note. Try again.");
      return;
    }
    field.defaultValue = saved;
    say(article, "Note saved.");
  }

  // The note is one line until more is typed into it.
  function grow(field) {
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight + field.offsetHeight - field.clientHeight}px`;
  }

  // ---- The clip ---------------------------------------------------------

  const SPEEDS = [1, 1.5, 2, 0.5];
  // Kept from one moment to the next.
  let speed = 1;
  let muted = false;

  const clipOf = (control) => one("[data-clip]", control.closest(".moment"));
  const clock = (seconds) => {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  };

  // Brings the transport in line with the clip.
  function paint(video) {
    const article = video.closest(".moment");
    if (!article) return;
    const length = Number.isFinite(video.duration) ? video.duration : 0;
    const playing = !video.paused && !video.ended;
    one("[data-now]", article).textContent = clock(video.currentTime);
    one("[data-length]", article).textContent = clock(length);

    const seek = one("[data-seek]", article);
    seek.max = String(length);
    seek.value = String(video.currentTime);
    seek.style.setProperty("--played", length ? `${(video.currentTime / length) * 100}%` : "0%");
    seek.setAttribute("aria-valuetext", `${clock(video.currentTime)} of ${clock(length)}`);

    const play = one("[data-play]", article);
    play.classList.toggle("is-playing", playing);
    play.setAttribute("aria-label", playing ? "Pause" : "Play");
    one("[data-mute]", article).setAttribute("aria-pressed", String(video.muted));
    one("[data-speed]", article).textContent = `${video.playbackRate}×`;
  }

  function togglePlay(video) {
    if (!video) return;
    if (video.paused || video.ended) video.play().catch(() => {});
    else video.pause();
  }

  function setSpeed(video) {
    speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
    video.defaultPlaybackRate = speed;
    video.playbackRate = speed;
  }

  function toggleMute(video) {
    muted = !video.muted;
    video.muted = muted;
  }

  function toggleFullScreen(video) {
    if (document.fullscreenElement) document.exitFullscreen();
    else video.requestFullscreen?.().catch(() => {});
  }

  // Plays the footage saved before or after the clip in the same player.
  function showFootage(link) {
    const video = clipOf(link);
    for (const other of all("[data-footage]", link.closest(".footage"))) other.removeAttribute("aria-current");
    link.setAttribute("aria-current", "true");
    video.src = link.href;
    video.play().catch(() => {});
  }

  // Fits what the script looks after to markup that has just arrived.
  function dress() {
    for (const field of all("[data-note]")) grow(field);
    for (const video of all("[data-clip]")) {
      video.defaultPlaybackRate = speed;
      video.playbackRate = speed;
      video.muted = muted;
      paint(video);
    }
  }

  // ---- Switches, the watchlist, shutting down --------------------------

  // Pressing the half that is not lit sets the switch to it.
  async function flip(half) {
    if (half.getAttribute("aria-pressed") === "true") return;
    const group = half.closest("[data-switch]");
    const response = await post(`${group.dataset.switch}?enabled=${half.dataset.enabled}`);
    if (!response || !response.ok) {
      say(group, "Could not save the change. Try again.");
      return;
    }
    say(group, "");
    for (const other of all("button", group)) other.setAttribute("aria-pressed", String(other === half));
  }

  async function addChannel(form) {
    const name = form.elements.slug.value.trim();
    if (!name) return;
    say(form, `Adding ${name}.`);
    const response = await post(`/channels?slug=${encodeURIComponent(name)}`);
    if (response && response.ok) {
      location.reload();
    } else if (response && response.status === 404) {
      say(form, `No channel called ${name} on Kick. Check the spelling.`);
    } else {
      say(form, `Could not add ${name}. The server log says why.`);
    }
  }

  // Shutting down is asked for twice: the button, then the same word again
  // next to a sentence that says what it will do.
  function askToShutDown(asking) {
    const section = one("[data-shutdown]");
    one("[data-shutdown-ask]", section).hidden = asking;
    one("[data-shutdown-confirm]", section).hidden = !asking;
    // Cancel takes the focus, so a stray Enter or Space backs out.
    one(asking ? "[data-shutdown-cancel]" : "[data-shutdown-open]", section).focus();
  }

  async function shutDown(button) {
    const section = button.closest("[data-shutdown]");
    button.disabled = true;
    const response = await post("/shutdown");
    if (!response || !response.ok) {
      button.disabled = false;
      say(section, "Could not reach the service. It may have stopped already.");
      return;
    }
    const pending = (await response.json()).pending_work_count;
    one("[data-shutdown-confirm]", section).hidden = true;
    say(
      section,
      pending
        ? `Shutting down. Waiting for ${plural(pending, "job")} to finish (clips being cut or analysed).`
        : "Shutting down. This page stops answering once the service has exited.",
    );
  }

  // ---- Wiring -----------------------------------------------------------

  const onClick = [
    ["a.ch-row", (row, event) => isPlainClick(event) && (event.preventDefault(), openMoment(row, { play: true }))],
    ["[data-rate]", (key) => rate(key.closest(".moment"), Number(key.dataset.rate))],
    ["[data-tag]", setTag],
    ["[data-switch] button", flip],
    ["[data-play]", (button) => togglePlay(clipOf(button))],
    ["[data-speed]", (button) => setSpeed(clipOf(button))],
    ["[data-mute]", (button) => toggleMute(clipOf(button))],
    ["[data-fullscreen]", (button) => toggleFullScreen(clipOf(button))],
    ["a[data-footage]", (link, event) => isPlainClick(event) && (event.preventDefault(), showFootage(link))],
    // In full screen the browser's own controls are showing and already do this.
    ["[data-clip]", (video) => video.controls || togglePlay(video)],
    ["[data-shutdown-open]", () => askToShutDown(true)],
    ["[data-shutdown-cancel]", () => askToShutDown(false)],
    ["[data-shutdown-go]", shutDown],
  ];

  document.addEventListener("click", (event) => {
    for (const [selector, handle] of onClick) {
      const element = event.target.closest(selector);
      if (!element) continue;
      handle(element, event);
      // A control pressed with the mouse does not keep the focus, so Space
      // goes back to playing the clip instead of pressing the control again.
      if (event.detail > 0 && element.matches("a, button")) element.blur();
      return;
    }
  });

  document.addEventListener("dblclick", (event) => {
    const video = event.target.closest("[data-clip]");
    if (video) toggleFullScreen(video);
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target.matches("[data-note]")) {
      // Enter finishes the note (Shift+Enter starts a new line in it), and
      // so does Escape; leaving the field is what saves it.
      if (event.key === "Escape" || (event.key === "Enter" && !event.shiftKey)) {
        event.preventDefault();
        target.blur();
      }
      return;
    }
    if (event.repeat || target.matches("textarea, select, input:not([type=range])") || !one(".review")) return;

    const key = event.key.toLowerCase();
    if (key === "j") move(1);
    else if (key === "k") move(-1);
    else if (key.length === 1 && key >= "1" && key <= "5") rateFromKeyboard(Number(key));
    else if (key === " " && !target.matches("button, a.ch-btn, a.ch-tab")) {
      event.preventDefault();
      togglePlay(one("[data-clip]"));
    }
  });

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (target.matches("[data-seek]")) {
      clipOf(target).currentTime = Number(target.value);
    } else if (target.matches("[data-note]")) {
      grow(target);
      say(target, "");
    }
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-note]")) saveNote(target);
    else if (target.matches("[data-submit-on-change]")) target.form.requestSubmit();
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-add-channel]");
    if (!form) return;
    event.preventDefault();
    addChannel(form);
  });

  // Media events do not bubble, so they are caught on the way down.
  for (const type of ["loadedmetadata", "durationchange", "timeupdate", "play", "pause", "ended", "emptied", "volumechange", "ratechange"]) {
    document.addEventListener(type, (event) => event.target.matches?.("[data-clip]") && paint(event.target), true);
  }

  // The page's own transport is not on screen in full screen, so the
  // browser's controls stand in for it for as long as that lasts.
  document.addEventListener("fullscreenchange", () => {
    for (const video of all("[data-clip]")) video.controls = document.fullscreenElement === video;
  });

  window.addEventListener("pagehide", () => saveNote());

  dress();
  currentRow()?.scrollIntoView({ block: "nearest" });
  if (one("[data-news]")) setInterval(checkForNew, NEWS_INTERVAL_MS);
})();
