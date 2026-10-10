// Clip Hunter dashboard.
//
// The pages are rendered on the server and work as plain pages. This script
// makes them quick to use: it opens a moment from the queue without loading
// the page again, saves a rating, tag or note as it is set, drives the clip
// player from the strip under it, replays chat in step with the clip, and
// runs the review from the keyboard (1 to 5 rate, J and K move, Space plays).
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
      await saveTitle();
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

  // Saves the moment's name if it was changed, and puts it on the moment's
  // row in the queue - or, if the name was taken away, what the row said
  // before it had one.
  async function saveTitle(field = one("[data-title]")) {
    if (!field || field.value === field.defaultValue) return;
    const article = field.closest(".moment");
    const response = await post(`/moments/${article.dataset.moment}/title`, { title: field.value });
    if (!response || !response.ok) {
      say(article, "Could not save the name. Try again.");
      return;
    }
    const title = (await response.json()).title || "";
    field.value = title;
    field.defaultValue = title;
    say(article, "");
    const what = one(`a.ch-row[data-moment="${article.dataset.moment}"] .ch-what`);
    if (!what) return;
    what.textContent = title ? `${title}${"noClip" in what.dataset ? ", no clip" : ""}` : what.dataset.unnamed;
  }

  // The note is one line until more is typed into it.
  function grow(field) {
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight + field.offsetHeight - field.clientHeight}px`;
  }

  // ---- The clip and its timeline ---------------------------------------
  //
  // The strip under the clip is a timeline over the clip's file, which holds
  // more than the clip: the footage that led up to it comes first and what
  // followed comes after. Times on the strip are clip time - seconds from
  // the clip's first frame, negative before it - so the player's own time
  // is clip time plus however long the footage before the clip runs.

  const SPEEDS = [1, 1.5, 2, 0.5];
  // Kept from one moment to the next - the volume from one visit to the next
  // as well, where the browser lets the page remember it.
  const VOLUME_KEY = "clip-hunter-volume";
  let speed = 1;
  let muted = false;
  let volume = 1;
  try {
    const kept = Number(localStorage.getItem(VOLUME_KEY) ?? 1);
    if (kept >= 0 && kept <= 1) volume = kept;
  } catch {
    // no storage here: the volume starts out full
  }

  const clipOf = (control) => one("[data-clip]", control.closest(".moment"));
  const clock = (seconds) => {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  };

  // What the strip of a moment covers, or null for a clip that has none.
  function spanOf(video) {
    const trace = one("[data-trace]", video.closest(".moment"));
    if (!trace || !one("[data-strip]", trace)) return null;
    return {
      start: Number(trace.dataset.start),
      end: Number(trace.dataset.end),
      clip: Number(trace.dataset.clipSeconds),
    };
  }

  // How long the footage before the clip runs in the player's file.
  const leadOf = (video) => Number(video.dataset.lead) || 0;

  // Where the player is, in clip time.
  function clipTime(video) {
    return video.currentTime - leadOf(video);
  }

  // A clip time the way the page writes it: time into the clip, or how long
  // before its start or after its end.
  function timeWords(time, span) {
    if (span && time < 0) return `−${clock(Math.ceil(-time))}`;
    if (span && time > span.clip) return `+${clock(time - span.clip)}`;
    return clock(time);
  }

  function place(video, time) {
    video.currentTime = Math.max(0, time + leadOf(video));
  }

  // Moves the player to a clip time. The strip can show more of chat than
  // there is footage for; a time beyond the footage is taken to its nearest
  // end.
  function goTo(video, time, { play } = {}) {
    const span = spanOf(video);
    if (!span) return;
    const lead = leadOf(video);
    const last = Number.isFinite(video.duration) ? video.duration - lead : span.end;
    place(video, Math.min(Math.max(time, -lead), last));
    if (play) video.play().catch(() => {});
  }

  // Chat lines appear as the clip reaches them, the newest at the bottom.
  function syncChat(article, time, words) {
    const box = one("[data-chat]", article);
    if (!box) return;
    let changed = false;
    for (const line of box.children) {
      if (line.dataset.at === undefined) continue;
      const later = Number(line.dataset.at) > time;
      if (line.hidden !== later) {
        line.hidden = later;
        changed = true;
      }
    }
    if (changed) box.scrollTop = box.scrollHeight;
    one("[data-chat-foot]", article).textContent = box.querySelector("[data-at]") ? `In step with the clip, at ${words}` : "";
  }

  // Brings the transport, the playhead and the chat in line with the player.
  function paint(video) {
    const article = video.closest(".moment");
    if (!article) return;
    const span = spanOf(video);
    const time = clipTime(video);
    const words = timeWords(time, span);
    const length = span ? span.clip : Number.isFinite(video.duration) ? video.duration : 0;
    const playing = !video.paused && !video.ended;

    one("[data-now]", article).textContent = words;
    one("[data-length]", article).textContent = clock(length);
    const play = one("[data-play]", article);
    play.classList.toggle("is-playing", playing);
    play.setAttribute("aria-label", playing ? "Pause" : "Play");
    one("[data-mute]", article).setAttribute("aria-pressed", String(video.muted));
    const heard = video.muted ? 0 : video.volume;
    const slider = one("[data-volume]", article);
    slider.value = String(heard);
    slider.style.setProperty("--level", `${heard * 100}%`);
    slider.setAttribute("aria-valuetext", `${Math.round(heard * 100)} percent`);
    one("[data-speed]", article).textContent = `${video.playbackRate}×`;
    if (!span) return;

    const strip = one("[data-strip]", article);
    const left = `${Math.min(Math.max((time - span.start) / (span.end - span.start), 0), 1) * 100}%`;
    one("[data-playhead]", strip).style.left = left;
    const flag = one("[data-flag]", strip);
    flag.style.setProperty("--at", left);
    flag.textContent = words;
    strip.setAttribute("aria-valuenow", time.toFixed(1));
    strip.setAttribute("aria-valuetext", `${words} of ${clock(span.clip)}`);
    syncChat(article, time, words);
  }

  function togglePlay(video) {
    if (!video) return;
    // Played to the end of the footage after the clip, it starts over at
    // the clip, not at the footage before it that the file opens with.
    if (video.ended) place(video, 0);
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
    // Taking the mute off a clip that was turned all the way down has to
    // make it heard.
    if (!muted && volume === 0) setVolume(video, 0.5);
  }

  // Dragging the volume up from silence takes the mute off; all the way
  // down is the same as muted.
  function setVolume(video, value) {
    volume = Math.min(Math.max(value, 0), 1);
    muted = volume === 0;
    video.volume = volume;
    video.muted = muted;
    try {
      localStorage.setItem(VOLUME_KEY, String(volume));
    } catch {
      // kept for this page only, then
    }
  }

  function toggleFullScreen(video) {
    if (document.fullscreenElement) document.exitFullscreen();
    else video.requestFullscreen?.().catch(() => {});
  }

  // ---- The strip as a scrubber ------------------------------------------

  // The clip time under the pointer, and how far along the strip that is.
  function pointOn(strip, event) {
    const box = strip.getBoundingClientRect();
    const share = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
    const trace = strip.closest("[data-trace]");
    const start = Number(trace.dataset.start);
    return { share, time: start + share * (Number(trace.dataset.end) - start) };
  }

  // Under the pointer: a hairline, and in the legend what chat did in that second.
  function readOut(strip, event) {
    const { share, time } = pointOn(strip, event);
    const trace = strip.closest("[data-trace]");
    const line = one("[data-hover]", strip);
    line.hidden = false;
    line.style.left = `${share * 100}%`;
    const readout = one("[data-readout]", trace);
    if (!readout) return;
    const second = Math.floor(time - Number(trace.dataset.start));
    const messages = Number(strip.dataset.all.split(",")[second] || 0);
    const laughing = Number(strip.dataset.laugh.split(",")[second] || 0);
    const when = time < 0 ? `${Math.ceil(-time)} s before` : time > Number(trace.dataset.clipSeconds) ? `${Math.floor(time - Number(trace.dataset.clipSeconds))} s after` : clock(time);
    readout.textContent = `${when}: ${plural(messages, "message")} a second${laughing ? `, ${laughing} laughing` : ""}`;
  }

  function stopReadOut(strip) {
    one("[data-hover]", strip).hidden = true;
    const readout = one("[data-readout]", strip.closest("[data-trace]"));
    if (readout) readout.textContent = "";
  }

  // Arrow keys step through the footage, Home and End go to the clip's ends.
  const STRIP_KEYS = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1, PageDown: -10, PageUp: 10 };

  function stripKey(strip, event) {
    const video = clipOf(strip);
    const span = spanOf(video);
    let time;
    if (event.key === "Home") time = 0;
    else if (event.key === "End") time = span.clip;
    else if (event.key in STRIP_KEYS) time = clipTime(video) + STRIP_KEYS[event.key] * (event.shiftKey ? 5 : 1);
    else return false;
    event.preventDefault();
    goTo(video, Math.min(Math.max(time, span.start), span.end));
    return true;
  }

  // ---- Where chat sits against the picture ------------------------------

  let shifting = false;

  // Moves a channel's chat earlier or later against its clips, by changing
  // how far behind the broadcast its viewers are taken to be. The trace and
  // the chat lines are drawn on the server, so the moment is fetched again
  // and those parts of it swapped in around the player, which plays on.
  async function shiftChat(button) {
    if (shifting) return;
    shifting = true;
    const article = button.closest(".moment");
    const seconds = Number(button.closest("[data-chat-delay]").dataset.chatDelay) + Number(button.dataset.chatShift);
    let markup = null;
    try {
      const saved = await post(`/channels/${encodeURIComponent(article.dataset.channel)}/chat_delay?seconds=${seconds}`);
      if (saved && saved.ok) {
        const response = await fetch(`/dashboard/moments/${article.dataset.moment}`);
        if (response.ok) markup = await response.text();
      }
    } catch {
      // handled below, like any other answer that is not the moment
    } finally {
      shifting = false;
    }
    if (!article.isConnected) return; // another moment was opened meanwhile
    if (markup === null) {
      say(article, "Could not move the chat. Try again.");
      return;
    }
    say(article, "");

    const held = document.activeElement === button;
    const fresh = new DOMParser().parseFromString(markup, "text/html");
    for (const part of ["[data-trace]", "[data-chat]", "[data-chat-delay]"]) {
      const now = one(part, fresh);
      const old = one(part, article);
      if (now && old) old.replaceWith(now);
    }
    if (held) one(`[data-chat-shift="${button.dataset.chatShift}"]`, article)?.focus();
    const video = one("[data-clip]", article);
    if (video) paint(video);
  }

  // Fits what the script looks after to markup that has just arrived.
  function dress() {
    for (const field of all("[data-note]")) grow(field);
    const video = one("[data-clip]");
    if (!video) return;
    video.defaultPlaybackRate = speed;
    video.playbackRate = speed;
    video.volume = volume;
    video.muted = muted;
    paint(video);
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
    ["[data-chat-shift]", shiftChat],
    ["[data-switch] button", flip],
    ["[data-play]", (button) => togglePlay(clipOf(button))],
    ["[data-speed]", (button) => setSpeed(clipOf(button))],
    ["[data-mute]", (button) => toggleMute(clipOf(button))],
    ["[data-fullscreen]", (button) => toggleFullScreen(clipOf(button))],
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

  // Pressing on the strip goes there, and so does dragging along it; a
  // pointer that is only passing over it gets a readout instead.
  document.addEventListener("pointerdown", (event) => {
    const strip = event.target.closest("[data-strip]");
    if (!strip || event.button !== 0) return;
    strip.setPointerCapture(event.pointerId);
    goTo(clipOf(strip), pointOn(strip, event).time);
  });

  document.addEventListener("pointermove", (event) => {
    const strip = event.target.closest("[data-strip]");
    if (!strip) return;
    if (strip.hasPointerCapture(event.pointerId)) goTo(clipOf(strip), pointOn(strip, event).time);
    readOut(strip, event);
  });

  document.addEventListener("pointerout", (event) => {
    const strip = event.target.closest("[data-strip]");
    if (strip && !strip.contains(event.relatedTarget)) stopReadOut(strip);
  });

  document.addEventListener("dblclick", (event) => {
    const video = event.target.closest("[data-clip]");
    if (video) toggleFullScreen(video);
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target.matches("[data-title]")) {
      // Enter finishes the name and so does Escape; leaving the field saves it.
      if (event.key === "Escape" || event.key === "Enter") {
        event.preventDefault();
        target.blur();
      }
      return;
    }
    if (target.matches("[data-note]")) {
      // Enter finishes the note (Shift+Enter starts a new line in it), and
      // so does Escape; leaving the field is what saves it.
      if (event.key === "Escape" || (event.key === "Enter" && !event.shiftKey)) {
        event.preventDefault();
        target.blur();
      }
      return;
    }
    if (target.matches("[data-strip]") && stripKey(target, event)) return;
    if (event.repeat || target.matches("input, textarea, select") || !one(".review")) return;

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
    if (target.matches("[data-note]")) {
      grow(target);
      say(target, "");
    } else if (target.matches("[data-volume]")) {
      setVolume(clipOf(target), Number(target.value));
    }
  });

  // A slider let go of with the mouse does not keep the focus, so Space goes
  // back to playing the clip.
  document.addEventListener("pointerup", (event) => {
    if (event.target.matches?.("[data-volume]")) event.target.blur();
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-note]")) saveNote(target);
    else if (target.matches("[data-title]")) saveTitle(target);
    else if (target.matches("[data-submit-on-change]")) target.form.requestSubmit();
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-add-channel]");
    if (!form) return;
    event.preventDefault();
    addChannel(form);
  });

  // Media events do not bubble, so they are caught on the way down.
  const onMedia = (type, handle) =>
    document.addEventListener(type, (event) => event.target.matches?.("[data-clip]") && handle(event.target), true);
  for (const type of ["loadedmetadata", "durationchange", "timeupdate", "seeked", "play", "pause", "ended", "emptied", "volumechange", "ratechange"]) {
    onMedia(type, paint);
  }

  // The page's own transport is not on screen in full screen, so the
  // browser's controls stand in for it for as long as that lasts.
  document.addEventListener("fullscreenchange", () => {
    for (const video of all("[data-clip]")) video.controls = document.fullscreenElement === video;
  });

  window.addEventListener("pagehide", () => {
    saveNote();
    saveTitle();
  });

  dress();
  currentRow()?.scrollIntoView({ block: "nearest" });
  if (one("[data-news]")) setInterval(checkForNew, NEWS_INTERVAL_MS);
})();
