// Clip Hunter dashboard.
//
// The pages are rendered on the server and work as plain pages. This script
// makes them quick to use: it opens a moment from the queue without loading
// the page again, saves a rating, tag or note as it is set, drives the clip
// player from the strip under it, replays chat in step with the clip, runs
// the review from the keyboard (1 to 5 rate, J and K move, Space plays), has
// the dialog a stretch of footage is cut out for an editor in - a moment's or
// a recording's - saves the Settings page's form, starts and stops the
// recording of a whole stream, replays a recording's chat beside it, puts
// what chat did over all of it under it, looks for moments in it, and keeps
// the Health page saying what holds now.
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

  // Shows the lines of a box of chat that had arrived by a clip time and
  // no others, the newest at the bottom.
  function reveal(box, time) {
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
  }

  // Chat lines appear as the clip reaches them - moved by however far the
  // slider under them has been dragged from what is saved, so where chat
  // will sit shows while it is being set.
  function syncChat(article, time, words) {
    const box = one("[data-chat]", article);
    if (!box) return;
    const delay = one("[data-chat-delay]", article);
    const later = delay ? Number(one("[data-chat-delay-slider]", delay).value) + Number(delay.dataset.chatDelay) : 0;
    reveal(box, time - later);
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

  // What the slider under the chat says while it is dragged: the lines
  // follow at once, the words say where they are being put.
  function previewChatShift(slider) {
    const later = Number(slider.value);
    one("[data-chat-delay-words]", slider.closest("[data-chat-delay]")).textContent = later
      ? `Chat ${Math.abs(later)} s ${later > 0 ? "later" : "earlier"}`
      : "Chat as it arrived";
    const video = clipOf(slider);
    if (video) paint(video);
  }

  // Moves a channel's chat earlier or later against its clips, by changing
  // how far behind the broadcast its viewers are taken to be - once the
  // slider is let go of. The trace and the chat lines are drawn on the
  // server, so the moment is fetched again and those parts of it swapped in
  // around the player, which plays on.
  async function shiftChat(slider) {
    if (shifting) return;
    shifting = true;
    const article = slider.closest(".moment");
    // Later is to the right; the delay is how much earlier chat is put.
    const seconds = -Number(slider.value);
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

    const held = document.activeElement === slider;
    const fresh = new DOMParser().parseFromString(markup, "text/html");
    for (const part of ["[data-trace]", "[data-chat]", "[data-chat-delay]"]) {
      const now = one(part, fresh);
      const old = one(part, article);
      if (now && old) old.replaceWith(now);
    }
    if (held) one("[data-chat-delay-slider]", article)?.focus();
    const video = one("[data-clip]", article);
    if (video) paint(video);
  }

  // ---- Trimming ---------------------------------------------------------
  //
  // The Trim dialog cuts a stretch of footage out for an editor, with its
  // chat as a video to lay over it if that is wanted. It has a player of
  // its own on the same file and a strip with a handle at either end of the
  // stretch; either end can be typed as a time as well. The cutting is done
  // on the server and takes a while with chat; the dialog asks now and then
  // how far it has got, for as long as it is open.
  //
  // A moment's dialog shows all of the moment's footage, on a copy of the
  // strip under the clip, and its times are clip time. A recording's is
  // "windowed": hours do not fit a strip a handle could be set on, so its
  // strip shows a few minutes of the file around the stretch - a window,
  // which follows the stretch wherever it is taken - and its times are
  // seconds of the file, written as the recording's player writes them.

  const TRIM_POLL_MS = 2000;
  const TRIM_BUSY = ["cutting", "chat"];
  // Past its end by less than this, the player ran there; by more, it was put there.
  const TRIM_RAN_PAST_SECONDS = 1;
  // An end of the stretch closer than this share of the window's length to
  // an end of the window has no room left to be dragged: the window is set
  // around the stretch again.
  const TRIM_WINDOW_EDGE = 0.03;
  // A window this many times as long as the stretch would be given anew is
  // set anew: the stretch was made a good deal shorter.
  const TRIM_WINDOW_TOO_WIDE = 2;
  // What the dialog said of a time it could not read, while it says it.
  let trimNotATime = "";
  let trimWatch = null;
  let trimDrag = null; // the handle that is held: "from" or "to"
  let trimGrab = 0; // how far from the handle's time it was taken hold of
  let trimWas = 0; // where the dialog's player was when last looked at

  const trimOf = (control) => control.closest("[data-trim]");
  const trimClip = (dialog) => one("[data-trim-clip]", dialog);
  const clamp = (value, least, most) => Math.min(Math.max(value, least), most);
  const twoDigits = (number) => String(number).padStart(2, "0");

  // What the dialog's strip covers, where the clip lies on it, and how far
  // the footage - what there is to cut from - reaches either way. A window
  // on a recording covers less than there is to cut from.
  function trimSpan(dialog) {
    const video = trimClip(dialog);
    const lead = Number(dialog.dataset.lead) || 0;
    const start = Number(dialog.dataset.start);
    const end = Number(dialog.dataset.end);
    const windowed = "window" in dialog.dataset;
    const last = Number.isFinite(video.duration) ? video.duration - lead : Number(dialog.dataset.last);
    return {
      start,
      end,
      lead,
      windowed,
      clip: windowed ? last : Number(dialog.dataset.clip),
      first: windowed ? 0 : Math.max(-lead, start),
      last: windowed ? last : Math.min(last, end),
    };
  }

  // The stretch that is set, and where the dialog's player is.
  const trimAt = (dialog) => ({ from: Number(dialog.dataset.from), to: Number(dialog.dataset.to) });
  const trimTime = (dialog) => trimClip(dialog).currentTime - (Number(dialog.dataset.lead) || 0);

  // A time to the tenth of a second, the way the page writes it: a clip
  // time with a sign outside the clip, a recording's with its hours.
  function trimWords(time, span) {
    if (span.windowed) {
      const tenths = Math.round(Math.max(time, 0) * 10);
      return `${Math.floor(tenths / 36000)}:${twoDigits(Math.floor(tenths / 600) % 60)}:${twoDigits(Math.floor(tenths / 10) % 60)}.${tenths % 10}`;
    }
    const outside = time < 0 ? "−" : time > span.clip ? "+" : "";
    const tenths = Math.round(Math.abs(outside === "+" ? time - span.clip : time) * 10);
    return `${outside}${Math.floor(tenths / 600)}:${twoDigits(Math.floor(tenths / 10) % 60)}.${tenths % 10}`;
  }

  // A time as it is typed: 1:23:45, 12:05.5, 75 or 75,5 - seconds, with
  // minutes and hours in front of them if there are any. As the page writes
  // a clip time, a minus in front counts back from the clip's start and a
  // plus on from its end. Null for what is not a time.
  function typedTime(text, span) {
    const match = /^([+\-−–]?)\s*(?:(?:(\d+):)?(\d+):)?(\d+(?:[.,]\d+)?)$/.exec(text.trim());
    if (!match) return null;
    const [, sign, hours, minutes, seconds] = match;
    const time = Number(hours || 0) * 3600 + Number(minutes || 0) * 60 + Number(seconds.replace(",", "."));
    if (sign === "+") return span.clip + time;
    return sign ? -time : time;
  }

  // The clip time under the pointer on the dialog's strip - or beyond it,
  // for a pointer that has been dragged off its end.
  function trimPoint(strip, event) {
    const box = strip.getBoundingClientRect();
    const span = trimSpan(trimOf(strip));
    return span.start + ((event.clientX - box.left) / box.width) * (span.end - span.start);
  }

  function seekTrim(dialog, time) {
    const span = trimSpan(dialog);
    trimClip(dialog).currentTime = clamp(time, span.first, span.last) + span.lead;
  }

  // Moves one end of the stretch, and the player with it, so the picture
  // shows what the stretch now starts or ends on. The ends keep to the
  // footage and stay the shortest stretch apart.
  function moveTrim(dialog, which, time) {
    const span = trimSpan(dialog);
    const least = Number(dialog.dataset.min);
    let { from, to } = trimAt(dialog);
    if (which === "from") from = Math.max(span.first, Math.min(time, to - least));
    else to = Math.min(span.last, Math.max(time, from + least));
    dialog.dataset.from = from.toFixed(2);
    dialog.dataset.to = to.toFixed(2);
    const video = trimClip(dialog);
    video.pause();
    video.currentTime = (which === "from" ? from : to) + span.lead;
    paintTrim(dialog);
    // While a handle is held the strip stays as it is under it.
    if (trimDrag === null) fitTrimWindow(dialog);
  }

  // An end of the stretch as it was typed into its field. What is not a
  // time is said so, and the field goes back to what the end is.
  function typeTrim(field) {
    const dialog = trimOf(field);
    const span = trimSpan(dialog);
    const which = field.dataset.trimType;
    const said = () => trimWords(trimAt(dialog)[which], span);
    if (field.value.trim() === said()) return;
    const time = typedTime(field.value, span);
    const state = one("[data-trim-state]", dialog);
    if (time === null) {
      trimNotATime = `"${field.value.trim()}" is not a time. Write it like ${span.windowed ? "1:23:45 or 83:45.5" : "0:12.5"}.`;
      state.textContent = trimNotATime;
    } else {
      moveTrim(dialog, which, time);
      // Put right, it is no longer said - what the cut stands at is.
      if (state.textContent === trimNotATime) askTrim(dialog);
    }
    field.value = said();
  }

  // The window a recording's dialog shows: as much of the file as the
  // dialog was given to show at least, more for a long stretch, with the
  // stretch in its middle where the file's ends allow.
  function trimWindow(dialog, from, to) {
    const whole = Number(dialog.dataset.last);
    const length = Math.min(whole, Math.max(Number(dialog.dataset.window), (to - from) * 1.5 + 60));
    const start = clamp((from + to) / 2 - length / 2, 0, whole - length);
    return { start, end: start + length };
  }

  // Sets the window around the stretch and has it drawn - if an end of the
  // stretch has come to an end of the window or left it, if the stretch
  // has become so short that the window shows far more than it needs to
  // (the handles would sit on top of each other), or `anew`.
  function fitTrimWindow(dialog, { anew = false } = {}) {
    if (!("window" in dialog.dataset)) return;
    const { from, to } = trimAt(dialog);
    const start = Number(dialog.dataset.start);
    const end = Number(dialog.dataset.end);
    const edge = (end - start) * TRIM_WINDOW_EDGE;
    const roomBefore = start <= 0 || from >= start + edge;
    const roomAfter = end >= Number(dialog.dataset.last) || to <= end - edge;
    const fresh = trimWindow(dialog, from, to);
    const tooWide = end - start > TRIM_WINDOW_TOO_WIDE * (fresh.end - fresh.start);
    if (!anew && roomBefore && roomAfter && !tooWide) return;
    dialog.dataset.start = fresh.start.toFixed(1);
    dialog.dataset.end = fresh.end.toFixed(1);
    one("[data-trim-drawing]", dialog).replaceChildren();
    one("[data-trim-axis]", dialog).replaceChildren();
    paintTrim(dialog);
    drawTrimWindow(dialog);
  }

  // What chat did over the window, drawn by the service like the trace
  // under the recording. Without it the strip is blank paper, and the
  // handles work all the same.
  async function drawTrimWindow(dialog) {
    const { start, end } = dialog.dataset;
    let markup = null;
    try {
      const response = await fetch(`${dialog.dataset.stripUrl}?first=${start}&last=${end}`);
      if (response.ok) markup = await response.text();
    } catch {
      // not reachable just now
    }
    // The window may have been set elsewhere while this was on its way.
    if (!markup || dialog.dataset.start !== start || dialog.dataset.end !== end) return;
    const figure = one("[data-rec-trace]", new DOMParser().parseFromString(markup, "text/html"));
    if (!figure) return;
    const drawn = all("[data-rec-strip] > *", figure).filter((part) => !part.matches("[data-hover], [data-playhead], [data-flag]"));
    one("[data-trim-drawing]", dialog).replaceChildren(...drawn);
    one("[data-trim-axis]", dialog).replaceChildren(...all(".ch-axis > *", figure));
  }

  // Brings the dialog's strip, its readouts and its chat in line with the
  // stretch that is set and with the player.
  function paintTrim(dialog) {
    const span = trimSpan(dialog);
    const video = trimClip(dialog);
    const time = trimTime(dialog);
    let { from, to } = trimAt(dialog);
    // The file can turn out a little shorter than it was said to be.
    if (to > span.last) {
      to = span.last;
      dialog.dataset.to = to.toFixed(2);
    }

    const share = (at) => `${clamp((at - span.start) / (span.end - span.start), 0, 1) * 100}%`;
    const strip = one("[data-trim-strip]", dialog);
    strip.style.setProperty("--from", share(from));
    strip.style.setProperty("--to", share(to));
    strip.style.setProperty("--at", share(time));
    for (const [which, at, least, most] of [["from", from, span.first, to], ["to", to, from, span.last]]) {
      const handle = one(`[data-trim-handle="${which}"]`, strip);
      handle.setAttribute("aria-valuemin", least.toFixed(1));
      handle.setAttribute("aria-valuemax", most.toFixed(1));
      handle.setAttribute("aria-valuenow", at.toFixed(1));
      handle.setAttribute("aria-valuetext", trimWords(at, span));
      // A field that is being typed into is left alone.
      const field = one(`[data-trim-type="${which}"]`, dialog);
      if (document.activeElement !== field) field.value = trimWords(at, span);
    }
    const length = to - from;
    one("[data-trim-length]", dialog).textContent =
      length < 60 ? `${length.toFixed(1)} s` : `${Math.floor(length / 60)} min ${(length % 60).toFixed(1)} s`;
    one("[data-trim-now]", dialog).textContent = trimWords(time, span);

    const playing = !video.paused && !video.ended;
    const play = one("[data-trim-play]", dialog);
    play.classList.toggle("is-playing", playing);
    play.setAttribute("aria-label", playing ? "Pause" : "Play");

    // Chat as its video will have it: each line where it stands on the
    // page, moved by what the slider says. A recording's is fetched a
    // stretch at a time, as beside the recording.
    const pane = one("[data-trim-chat-pane]", dialog);
    if (!pane || pane.hidden) return;
    const shown = () => trimTime(dialog) - Number(one("[data-trim-shift]", dialog).value);
    if (span.windowed) followChat(pane, shown);
    else reveal(one("[data-trim-lines]", pane), shown());
  }

  // Played, the stretch stops at its end.
  function trimTick(video) {
    const dialog = trimOf(video);
    const time = trimTime(dialog);
    const { to } = trimAt(dialog);
    const ranPast = trimWas < to && time >= to && time - to < TRIM_RAN_PAST_SECONDS;
    trimWas = time;
    if (ranPast && !video.paused) {
      video.pause();
      video.currentTime = to + (Number(dialog.dataset.lead) || 0);
      trimWas = to;
    }
    paintTrim(dialog);
  }

  // Plays from where the player is - which can be outside the stretch, to
  // see what leads up to it or follows. From its end it starts over.
  function toggleTrimPlay(dialog) {
    const video = trimClip(dialog);
    if (!video.paused && !video.ended) {
      video.pause();
      return;
    }
    const { from, to } = trimAt(dialog);
    if (video.ended || Math.abs(trimTime(dialog) - to) < 0.05) {
      video.currentTime = from + (Number(dialog.dataset.lead) || 0);
      trimWas = from;
    }
    video.play().catch(() => {});
  }

  // Arrow keys nudge the handle that has the focus; I and O put the start
  // and the end where the player is; Space plays.
  const TRIM_KEYS = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1 };

  function trimKey(dialog, event) {
    const target = event.target;
    // In a field for a time the keys are for typing; Enter sets the end.
    if (target.matches("[data-trim-type]")) {
      if (event.key === "Enter") {
        event.preventDefault();
        typeTrim(target);
      }
      return;
    }
    const handle = target.closest("[data-trim-handle]");
    if (handle && event.key in TRIM_KEYS) {
      event.preventDefault();
      const which = handle.dataset.trimHandle;
      moveTrim(dialog, which, trimAt(dialog)[which] + TRIM_KEYS[event.key] * (event.shiftKey ? 1 : 0.1));
      return;
    }
    if (event.repeat || target.matches("input")) return;
    const key = event.key.toLowerCase();
    if (key === "i") moveTrim(dialog, "from", trimTime(dialog));
    else if (key === "o") moveTrim(dialog, "to", trimTime(dialog));
    else if (key === " " && !target.matches("button, a")) {
      event.preventDefault();
      toggleTrimPlay(dialog);
    }
  }

  function openTrim(button) {
    const article = button.closest(".moment, .recording");
    const dialog = one("[data-trim]", article);
    const windowed = dialog && "window" in dialog.dataset;
    const strip = one("[data-strip]", article);
    if (!dialog || !(windowed || strip)) return;
    const watched = one("[data-clip], [data-recording-clip]", article);
    watched.pause();

    const video = trimClip(dialog);
    if (!video.getAttribute("src")) {
      video.preload = "auto";
      video.src = video.dataset.src;
    }
    video.volume = watched.volume;
    video.muted = watched.muted;

    if (windowed) {
      // A recording: to begin with the stretch is the footage around where
      // it was being watched. Opened again from the same place, the dialog
      // is as it was left.
      const at = watched.currentTime;
      if (dialog.dataset.from === undefined || Math.abs(at - Number(dialog.dataset.openedAt)) > 1) {
        const whole = Number(dialog.dataset.last);
        const around = Number(dialog.dataset.around);
        const from = clamp(at - around, 0, Math.max(whole - 2 * around, 0));
        dialog.dataset.from = from.toFixed(2);
        dialog.dataset.to = Math.min(from + 2 * around, whole).toFixed(2);
      }
      dialog.dataset.openedAt = String(at);
    } else {
      // A moment: the strip and the chat in the dialog are copies of the
      // ones on the page, taken now - where chat sits may have been moved
      // since last time. The strip's note on the moment is left out: a
      // handle would cut it.
      const trace = strip.closest("[data-trace]");
      dialog.dataset.start = trace.dataset.start;
      dialog.dataset.end = trace.dataset.end;
      dialog.dataset.clip = trace.dataset.clipSeconds;
      const copies = (parts) => parts.map((part) => part.cloneNode(true));
      const drawn = all(":scope > *", strip).filter((part) => !part.matches("[data-hover], [data-playhead], [data-flag], .ch-note"));
      one("[data-trim-drawing]", dialog).replaceChildren(...copies(drawn));
      one("[data-trim-axis]", dialog).replaceChildren(...copies(all("[data-trace] .ch-axis > *", article)));
      one("[data-trim-lines]", dialog)?.replaceChildren(...copies(all("[data-chat] > [data-at]", article)));
    }

    dialog.showModal();
    // To begin with a moment's stretch is the clip, as it was cut.
    if (dialog.dataset.from === undefined) {
      const span = trimSpan(dialog);
      dialog.dataset.from = "0";
      dialog.dataset.to = Math.min(span.clip, span.last).toFixed(2);
    }
    trimWas = Number(dialog.dataset.from);
    video.currentTime = trimWas + (Number(dialog.dataset.lead) || 0);
    if (windowed) fitTrimWindow(dialog, { anew: true });
    else paintTrim(dialog);
    askTrim(dialog);
  }

  function leaveTrim(dialog) {
    trimClip(dialog).pause();
    clearTimeout(trimWatch);
  }

  // Whether the chat goes with the video: its lines are then shown beside
  // the picture, and can be moved against it and given a background.
  function wantTrimChat(box) {
    const dialog = trimOf(box);
    one("[data-trim-chat-pane]", dialog).hidden = !box.checked;
    one("[data-trim-shift]", dialog).disabled = !box.checked;
    one("[data-trim-background]", dialog).disabled = !box.checked;
    paintTrimBackground(dialog);
    paintTrim(dialog);
  }

  // The colour the chat video is to have behind its text, or null for
  // nothing there.
  function trimBackground(dialog) {
    const wanted = one("[data-trim-background]", dialog);
    return wanted && wanted.checked ? one("[data-trim-background-colour]", dialog).value : null;
  }

  // The chat beside the player shows the background the video will have.
  function paintTrimBackground(dialog) {
    const wanted = one("[data-trim-background]", dialog);
    one("[data-trim-background-colour]", dialog).disabled = wanted.disabled || !wanted.checked;
    one("[data-trim-chat-pane]", dialog).style.background = trimBackground(dialog) || "";
  }

  function shiftTrimChat(slider) {
    const dialog = trimOf(slider);
    const seconds = Number(slider.value);
    const words = one("[data-trim-shift-words]", dialog);
    words.textContent = seconds ? `Chat ${Math.abs(seconds)} s ${seconds > 0 ? "later" : "earlier"}` : words.dataset.rest;
    paintTrim(dialog);
  }

  // Says where the cut stands and offers what there is to download.
  function showTrim(dialog, trim) {
    const busy = TRIM_BUSY.includes(trim.state);
    one("[data-trim-state]", dialog).textContent = trim.words;
    for (const [part, url] of [["[data-trim-video]", trim.video_url], ["[data-trim-chat-video]", trim.chat_url]]) {
      const link = one(part, dialog);
      link.hidden = !url;
      if (url) link.href = url;
      else link.removeAttribute("href");
    }
    one("[data-trim-go]", dialog).disabled = busy;
    clearTimeout(trimWatch);
    if (busy) trimWatch = setTimeout(() => askTrim(dialog), TRIM_POLL_MS);
  }

  async function askTrim(dialog) {
    const still = () => dialog.isConnected && dialog.open;
    if (!still()) return;
    let trim = null;
    try {
      const response = await fetch(dialog.dataset.trim);
      if (response.ok) trim = await response.json();
    } catch {
      // not reachable just now
    }
    if (!still()) return;
    if (trim) showTrim(dialog, trim);
    else if (one("[data-trim-go]", dialog).disabled) {
      // One is being made: asked again.
      clearTimeout(trimWatch);
      trimWatch = setTimeout(() => askTrim(dialog), TRIM_POLL_MS);
    }
  }

  async function exportTrim(button) {
    const dialog = trimOf(button);
    const { from, to } = trimAt(dialog);
    const chat = one("[data-trim-chat]", dialog);
    const shift = one("[data-trim-shift]", dialog);
    button.disabled = true;
    const response = await post(dialog.dataset.trim, {
      start: from,
      end: to,
      chat: Boolean(chat && chat.checked),
      chat_shift: shift ? Number(shift.value) : 0,
      chat_background: chat ? trimBackground(dialog) : null,
    });
    if (!dialog.isConnected) return; // another moment was opened meanwhile
    if (!response || !response.ok) {
      button.disabled = false;
      // What can't be cut is said by the service: too long for its chat to
      // go with it, a disk too full.
      one("[data-trim-state]", dialog).textContent = response
        ? await problem(response, "Could not start the cut. Try again.")
        : "Could not reach the service.";
      return;
    }
    showTrim(dialog, await response.json());
  }

  // Fits what the script looks after to markup that has just arrived.
  function dress() {
    for (const field of all("[data-note]")) grow(field);
    for (const button of all("[data-trim-open], [data-find-moments]")) button.hidden = false;
    markFound();
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
    for (const other of all("button", group)) other.setAttribute("aria-pressed", String(other === half));
    // Some switches change what the page around them says.
    if (group.hasAttribute("data-switch-reloads")) reloadSettings();
    if (group.closest("[data-always-record]")) {
      // What the switch does is not on the page, so the service says it -
      // and a channel that is live may be recorded from this moment.
      say(group, (await response.json()).said || "");
      watchRecordings();
      return;
    }
    say(group, "");
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
        ? `Shutting down. Waiting for ${plural(pending, "job")} to finish (clips being cut or analysed, recordings being saved).`
        : "Shutting down. This page stops answering once the service has exited.",
    );
  }

  // ---- Stream recordings ------------------------------------------------
  //
  // A channel's whole stream is recorded from the Channels page, and what
  // was recorded is on the Recordings page. Neither reloads on its own:
  // while a recording runs or is being saved, the service is asked every
  // couple of seconds what it is doing, and the words on the page follow.
  // A channel that is recorded always starts being recorded without anyone
  // pressing anything, so a page that has one asks now and then even while
  // nothing is being recorded.

  const RECORDINGS_POLL_MS = 2000;
  const RECORDINGS_AWAITED_POLL_MS = 10000;
  let recordingsWatch = null;

  const recordingsAwaited = () =>
    Boolean(one('[data-always-record] [data-enabled="1"][aria-pressed="true"], [data-recordings-awaited]'));

  // What a channel's recording is doing, in the place the page has for it,
  // and whichever of the two buttons goes with that. `state` is null for a
  // channel with no recording running or being saved.
  function showRecording(place, state) {
    place.dataset.recordingState = state ? (state.running ? "running" : "saving") : "";
    one("[data-recording-words]", place).textContent = state ? state.words : "";
    const start = one("[data-recording-start]", place);
    const stop = one("[data-recording-stop]", place);
    if (start) start.hidden = Boolean(state && state.running);
    if (stop) stop.hidden = !(state && state.running);
  }

  // What the channels are doing right now, as the bar says it of the
  // service and the watchlist of each channel: the words, and a lit lamp
  // where a stream is coming in.
  function showNow(now) {
    const lamp = one("[data-now-lamp]");
    lamp.classList.toggle("ch-lamp-rec", now.live);
    lamp.textContent = now.words;
    one("[data-now-count]").textContent = now.count;
    for (const cell of all("[data-now]")) {
      const channel = now.channels[cell.dataset.now];
      if (!channel) continue;
      cell.firstElementChild.className = channel.live ? "ch-lamp ch-lamp-rec" : "ch-meta";
      cell.firstElementChild.textContent = channel.words;
    }
  }

  // The Recordings page as the service has it now: the list, and what is
  // open beside it - unless that is a video, which is left to play.
  async function reloadRecordings() {
    let fresh;
    try {
      const response = await fetch(location.href);
      if (!response.ok) return;
      fresh = new DOMParser().parseFromString(await response.text(), "text/html");
    } catch {
      return; // what is on screen stays
    }
    const list = one("[data-recordings]");
    const freshList = one("[data-recordings]", fresh);
    if (list && freshList) list.replaceWith(freshList);
    const open = one("[data-recording-open]");
    const freshOpen = one("[data-recording-open]", fresh);
    if (!open || !freshOpen || one("video", open)) return;
    open.replaceWith(freshOpen);
    // What has been saved meanwhile can be played, and has a trace.
    dress();
    loadRecordingTrace();
    watchFound();
  }

  async function checkRecordings() {
    let status;
    try {
      const response = await fetch("/recordings/status");
      if (!response.ok) return;
      status = await response.json();
    } catch {
      return; // restarting or down - the page keeps what it says
    }
    for (const place of all("[data-recording]")) {
      const state = status.channels[place.dataset.recording] || null;
      const saved = place.dataset.recordingState === "saving" && !state;
      showRecording(place, state);
      if (saved) say(place, `The recording of ${place.dataset.recording} is on the Recordings page.`);
    }
    for (const row of all("[data-recording-row]")) {
      const state = status.recordings[row.dataset.recordingRow];
      if (state) one("[data-recording-words]", row).textContent = state.words;
    }
    showNow(status.now);
    // The list reads differently: one was added, saved or removed.
    const list = one("[data-recordings]");
    if (list && list.dataset.mark !== status.mark) await reloadRecordings();
    if (Object.keys(status.recordings).length) {
      watchRecordings();
    } else {
      clearInterval(recordingsWatch);
      recordingsWatch = null;
    }
  }

  function watchRecordings() {
    if (recordingsWatch === null) recordingsWatch = setInterval(checkRecordings, RECORDINGS_POLL_MS);
  }

  async function setRecording(button, on) {
    const place = button.closest("[data-recording]");
    const channel = encodeURIComponent(place.dataset.recording);
    button.disabled = true;
    const response = await post(`/channels/${channel}/recording/${on ? "start" : "stop"}`);
    button.disabled = false;
    if (!response) {
      say(place, "Could not reach the service.");
      return;
    }
    if (!response.ok) {
      say(place, await problem(response, on ? "Could not start the recording." : "Could not stop the recording."));
      return;
    }
    const result = await response.json();
    showRecording(place, result.recording);
    say(place, result.said || "");
    watchRecordings();
    // The lamps follow at once, not at the next time of asking.
    checkRecordings();
  }

  // Deleting is asked for twice, like shutting down.
  function askToDelete(control, asking) {
    const group = control.closest("[data-delete]");
    one("[data-delete-open]", group).hidden = asking;
    one("[data-delete-confirm]", group).hidden = !asking;
    one(asking ? "[data-delete-cancel]" : "[data-delete-open]", group).focus();
  }

  async function deleteRecording(button) {
    button.disabled = true;
    // A file that is being played is held open, and can't be removed - by
    // the player on the page, or by the one in its Trim dialog.
    for (const video of all("[data-recording-clip], .recording [data-trim-clip]")) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    const response = await post(`/recordings/${button.dataset.deleteGo}/delete`);
    if (response && response.ok) {
      location.assign("/dashboard/recordings");
      return;
    }
    button.disabled = false;
    say(button, response ? await problem(response, "Could not delete the recording.") : "Could not reach the service.");
  }

  async function saveRecordingAgain(button) {
    button.disabled = true;
    const response = await post(`/recordings/${button.dataset.saveAgain}/save`);
    if (response && response.ok) {
      await reloadRecordings();
      watchRecordings();
      return;
    }
    button.disabled = false;
    say(button, response ? await problem(response, "Could not save the recording.") : "Could not reach the service.");
  }

  // A recording runs for hours, so its chat is not all on the page: the box
  // beside the video holds the lines of one stretch of it, and says which.
  // As the video nears the end of that stretch, or is moved out of it, the
  // lines around where it is now are fetched in their place.

  // How long before the end of the stretch the next one is asked for.
  const CHAT_AHEAD_SECONDS = 30;
  // How long the same place is left alone after it was asked about - for a
  // chat so busy that what came back did not reach where the video is.
  const CHAT_AGAIN_MS = 5000;
  // What each such box was last asked for, by what it is in - the box
  // itself is replaced with every stretch. There are two: the one beside
  // the recording and the one in its Trim dialog.
  const chatAsked = new WeakMap();

  // Shows the lines of the box in `holder` up to the second of the
  // recording that `now` gives - a function, as the answer may well be
  // another by the time lines have been fetched.
  async function followChat(holder, now) {
    const box = holder && one("[data-recording-chat]", holder);
    if (!box) return;
    const time = now();
    reveal(box, time);
    if (!chatAsked.has(holder)) chatAsked.set(holder, { at: -Infinity, when: 0, onItsWay: false });
    const asked = chatAsked.get(holder);
    const outside = time < Number(box.dataset.first) || time > Number(box.dataset.last);
    const nearEnd = !("whole" in box.dataset) && time > Number(box.dataset.last) - CHAT_AHEAD_SECONDS;
    const justAsked = Math.abs(time - asked.at) < CHAT_AHEAD_SECONDS && performance.now() - asked.when < CHAT_AGAIN_MS;
    if (!(outside || nearEnd) || asked.onItsWay || justAsked) return;

    Object.assign(asked, { at: time, when: performance.now(), onItsWay: true });
    let markup = null;
    try {
      const response = await fetch(`${box.dataset.url}?at=${Math.max(time, 0).toFixed(1)}`);
      if (response.ok) markup = await response.text();
    } catch {
      // the lines there are stay
    } finally {
      asked.onItsWay = false;
    }
    const fresh = markup && one("[data-recording-chat]", new DOMParser().parseFromString(markup, "text/html"));
    const current = one("[data-recording-chat]", holder);
    if (!fresh || !current) return;
    current.replaceWith(fresh);
    // The video may have been moved again while these were on their way.
    followChat(holder, now);
  }

  // Under a recording is what chat did over the whole of it (see
  // _recording_trace.html): hours on one strip, to find one's way about
  // them by. It is a scrubber like the strip under a clip, on the time of
  // the recording's file. Counting that much chat takes the service a
  // moment, so the page comes with a place for it and it is fetched here.

  // A time in a recording, as its player writes it.
  const longClock = (seconds) => {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return `${Math.floor(whole / 3600)}:${twoDigits(Math.floor(whole / 60) % 60)}:${twoDigits(whole % 60)}`;
  };

  // Where on the trace the pointer has the recording while it is dragged
  // along it: the player follows as fast as it can find the pictures.
  let recordingDrag = null;

  async function loadRecordingTrace() {
    const place = one("[data-rec-trace-load]");
    if (!place) return;
    place.hidden = false;
    let markup = null;
    try {
      const response = await fetch(place.dataset.recTraceLoad);
      if (response.ok) markup = await response.text();
    } catch {
      // said below
    }
    if (!place.isConnected) return; // another page of recordings by now
    const figure = markup && one("[data-rec-trace]", new DOMParser().parseFromString(markup, "text/html"));
    if (!figure) {
      one("[data-rec-trace-words]", place).textContent = "What chat did over the recording could not be worked out. The server log says why.";
      return;
    }
    place.replaceWith(figure);
    markFound();
    const video = one("[data-recording-clip]");
    if (video) paintRecording(video);
  }

  // Brings the chat beside a recording and the playhead on its trace in
  // line with its player.
  function paintRecording(video) {
    followChat(one(".recording .chat"), () => video.currentTime);
    const strip = one("[data-rec-strip]");
    if (!strip) return;
    const trace = strip.closest("[data-trace]");
    const start = Number(trace.dataset.start);
    const time = recordingDrag ?? video.currentTime;
    const left = `${clamp((time - start) / (Number(trace.dataset.end) - start), 0, 1) * 100}%`;
    one("[data-playhead]", strip).style.left = left;
    const flag = one("[data-flag]", strip);
    flag.style.setProperty("--at", left);
    flag.textContent = longClock(time);
    strip.setAttribute("aria-valuenow", time.toFixed(1));
    strip.setAttribute("aria-valuetext", longClock(time));
  }

  // Takes the recording to where the trace is pressed or dragged to. A
  // seek in a file of gigabytes takes a while, so one is not started
  // before the last has landed; letting go always goes where it was let go.
  function dragRecording(strip, event, { last = false } = {}) {
    const video = one("[data-recording-clip]");
    if (!video) return;
    recordingDrag = last ? null : pointOn(strip, event).time;
    if (last || !video.seeking) video.currentTime = pointOn(strip, event).time;
    paintRecording(video);
  }

  // A pace the way the readout says it: whole where it is one, to a tenth
  // where it is the average over a step.
  function paceWords(pace, noun) {
    const number = Number.isInteger(pace) || pace >= 10 ? String(Math.round(pace)) : pace.toFixed(1);
    return noun ? `${number} ${noun}${number === "1" ? "" : "s"}` : number;
  }

  // Under the pointer: a hairline, and in the legend the pace of chat in
  // the step it is over.
  function readOutRecording(strip, event) {
    const { share, time } = pointOn(strip, event);
    const trace = strip.closest("[data-trace]");
    const line = one("[data-hover]", strip);
    line.hidden = false;
    line.style.left = `${share * 100}%`;
    const readout = one("[data-readout]", trace);
    if (!readout) return;
    const step = Number(trace.dataset.step);
    const index = Math.floor((time - Number(trace.dataset.start)) / step);
    const pace = (counts) => Number(counts.split(",")[index] || 0) / step;
    const laughing = pace(strip.dataset.laugh);
    readout.textContent = `${longClock(time)}: ${paceWords(pace(strip.dataset.all), "message")} a second${laughing ? `, ${paceWords(laughing)} laughing` : ""}`;
  }

  // Arrow keys step through the recording, Page Up and Down by the minute,
  // Home and End go to its ends; with Shift, six times as far.
  const RECORDING_KEYS = { ArrowLeft: -5, ArrowRight: 5, ArrowDown: -5, ArrowUp: 5, PageDown: -60, PageUp: 60 };

  function recordingKey(strip, event) {
    const video = one("[data-recording-clip]");
    const trace = strip.closest("[data-trace]");
    let time;
    if (event.key === "Home") time = Number(trace.dataset.start);
    else if (event.key === "End") time = Number(trace.dataset.end);
    else if (event.key in RECORDING_KEYS) time = video.currentTime + RECORDING_KEYS[event.key] * (event.shiftKey ? 6 : 1);
    else return false;
    event.preventDefault();
    video.currentTime = clamp(time, Number(trace.dataset.start), Number(trace.dataset.end));
    return true;
  }

  // ---- Moments found in a recording --------------------------------------
  //
  // Under a recording is what looking for moments in its chat came to (see
  // _recording_moments.html): the service replays the chat through the
  // detector when asked, and lists what would have been a moment. A row's
  // time plays the recording from there, and its button has the service
  // make a moment of it, with a clip cut out of the recording. Both take the
  // service a while, so the section says what is being done and is fetched
  // again every couple of seconds until it is over.

  const FOUND_POLL_MS = 2000;
  let foundWatch = null;

  // Puts what was found and is no moment yet on the trace under the
  // recording, where the channel's moments are marked already: the same
  // mark, along the foot of the strip only, with a word for it in the
  // legend. Done again whenever either of the two is new on the page.
  function markFound() {
    const strip = one("[data-rec-strip]");
    if (!strip) return;
    const trace = strip.closest("[data-trace]");
    for (const old of all("[data-found-mark]", trace)) old.remove();
    const start = Number(trace.dataset.start);
    const whole = Number(trace.dataset.end) - start;
    const under = one("svg", strip);
    let marked = false;
    for (const row of all("[data-found]:not([data-found-moment])")) {
      const began = Number(row.dataset.began);
      const ended = Number(row.dataset.ended);
      if (!under || !(whole > 0) || ended <= start || began >= start + whole) continue;
      const mark = document.createElement("div");
      mark.className = "ch-moment is-found";
      mark.dataset.foundMark = "";
      mark.style.left = `${clamp((began - start) / whole, 0, 1) * 100}%`;
      mark.style.width = `${clamp((ended - began) / whole, 0, 1) * 100}%`;
      under.after(mark);
      marked = true;
    }
    const readout = one("[data-readout]", trace);
    if (!marked || !readout) return;
    const word = document.createElement("span");
    word.dataset.foundMark = "";
    word.innerHTML = '<span class="ch-chip ch-chip-found"><i></i></span>';
    word.append("found, not a moment yet");
    readout.before(word);
  }

  // The trace under the recording as the service draws it now: a moment
  // that was just made is one of the channel's, and marked as one.
  async function redrawRecordingTrace(url) {
    let markup = null;
    try {
      const response = await fetch(url);
      if (response.ok) markup = await response.text();
    } catch {
      // the one on the page stays
    }
    const fresh = markup && one("[data-rec-trace]", new DOMParser().parseFromString(markup, "text/html"));
    const current = one("[data-rec-trace]");
    if (!fresh || !current) return;
    current.replaceWith(fresh);
    markFound();
    const video = one("[data-recording-clip]");
    if (video) paintRecording(video);
  }

  // The section as the service has it now, in place of the one on the page.
  async function refreshFound() {
    const asked = one("[data-rec-moments]");
    if (!asked) return;
    let markup = null;
    try {
      const response = await fetch(asked.dataset.page);
      if (response.ok) markup = await response.text();
    } catch {
      // what is on the page stays, and is asked about again
    }
    const fresh = markup && one("[data-rec-moments]", new DOMParser().parseFromString(markup, "text/html"));
    const current = one("[data-rec-moments]");
    // Another recording may be open by the time the answer is here.
    if (!fresh || !current || current.dataset.url !== fresh.dataset.url) return;
    const made = all("[data-found-moment]", current).length;
    current.replaceWith(fresh);
    dress();
    // A moment that was made since is one of the channel's on the trace too.
    if (all("[data-found-moment]", fresh).length !== made) redrawRecordingTrace(fresh.dataset.trace);
    watchFound();
  }

  function watchFound() {
    const busy = Boolean(one("[data-rec-moments][data-busy]"));
    if (busy && foundWatch === null) {
      foundWatch = setInterval(refreshFound, FOUND_POLL_MS);
    } else if (!busy && foundWatch !== null) {
      clearInterval(foundWatch);
      foundWatch = null;
    }
  }

  // Asks the service for something to do with the recording's moments, and
  // shows where that stands; what it refuses is said in its own words.
  async function askFound(button, path, body, otherwise) {
    const section = button.closest("[data-rec-moments]");
    button.disabled = true;
    const response = await post(`${section.dataset.url}${path}`, body);
    if (!response || !response.ok) {
      button.disabled = false;
      say(button, response ? await problem(response, otherwise) : "Could not reach the service.");
      return;
    }
    await refreshFound();
  }

  const findMoments = (button) => askFound(button, "", undefined, "Could not look for moments.");

  // One row's moment, or - from the button over the list - all of them.
  function makeClips(button) {
    const key = button.dataset.makeClips;
    askFound(button, "/clips", key ? { moments: [Number(key)] } : {}, "Could not make the clip.");
  }

  function playFound(button) {
    const video = one("[data-recording-clip]");
    if (!video) return;
    video.currentTime = Number(button.dataset.foundGo);
    video.play().catch(() => {});
  }

  // ---- Settings ---------------------------------------------------------

  // The Settings page is one form, saved as a whole. Each setting has a
  // button beside it that puts back what it would be if it were left alone
  // (what is recommended, or for a channel what all channels have), and
  // what is in the form can be tried on stored chat before it is saved.

  const settingsOf = (control) => control.closest("[data-tuning]");
  const isList = (field) => field.dataset.kind === "words" || field.dataset.kind === "phrases";

  // What a setting's field says, the way the service takes it. A number
  // typed with a comma is the same number; one that is no number at all is
  // sent as nothing, and the service says what the field takes.
  function settingValue(field) {
    if (field.dataset.kind === "switch") return one('[aria-pressed="true"]', field).dataset.enabled === "1";
    if (isList(field)) return field.value;
    const typed = field.value.trim().replace(",", ".");
    return typed === "" || Number.isNaN(Number(typed)) ? null : Number(typed);
  }

  // Only what can be set is sent. A channel that goes by the settings for
  // all channels is shown those, locked: saved as its own they would take
  // the place of the ones it keeps for when it goes by its own again.
  const settingValues = (form) =>
    Object.fromEntries(
      all("[data-setting]", form)
        .filter((field) => !field.closest("fieldset:disabled"))
        .map((field) => [field.dataset.setting, settingValue(field)]),
    );

  // Marks the settings that are not what they would be if left alone, and
  // lets those be put back.
  function markSettings(form) {
    for (const field of all("[data-setting]", form)) {
      const row = field.closest(".set-row");
      const rest = one("[data-rest]", row);
      const value = settingValue(field);
      let atRest;
      if (field.dataset.kind === "switch") atRest = value === (rest.dataset.rest === "1");
      else if (isList(field)) atRest = value.trim() === rest.dataset.rest;
      else atRest = value === Number(rest.dataset.rest);
      row.classList.toggle("is-set", !atRest);
      rest.disabled = atRest;
    }
  }

  function settingsEdited(form) {
    markSettings(form);
    say(form, "Not saved yet.");
  }

  function putBack(rest) {
    const field = one("[data-setting]", rest.closest(".set-row"));
    if (field.dataset.kind === "switch") {
      for (const half of all("button", field)) half.setAttribute("aria-pressed", String(half.dataset.enabled === rest.dataset.rest));
    } else {
      field.value = rest.dataset.rest;
    }
  }

  // A switch among the settings is set like any other field: it is saved
  // with the form, not as it is pressed.
  function chooseSetting(half) {
    for (const other of all("button", half.closest("[data-setting]"))) other.setAttribute("aria-pressed", String(other === half));
    settingsEdited(settingsOf(half));
  }

  // What the service said was wrong, in its own words where it gave any.
  async function problem(response, otherwise) {
    try {
      const detail = (await response.json()).detail;
      return typeof detail === "string" ? detail : otherwise;
    } catch {
      return otherwise;
    }
  }

  // The page as the service has it now, in place of the one on screen:
  // after a save, or after a channel was switched to settings of its own.
  async function reloadSettings() {
    const page = one("[data-settings]");
    let fresh;
    try {
      const response = await fetch(location.href);
      if (!response.ok) return;
      fresh = one("[data-settings]", new DOMParser().parseFromString(await response.text(), "text/html"));
    } catch {
      return; // what is on screen stays, and still says what was typed
    }
    if (!fresh) return;
    const scrolled = page.scrollTop;
    page.replaceWith(fresh);
    fresh.scrollTop = scrolled;
  }

  async function saveSettings(form) {
    say(form, "Saving.");
    const response = await post(form.dataset.tuning, { values: settingValues(form) });
    if (!response) {
      say(form, "Could not reach the service. Nothing was saved.");
      return;
    }
    if (!response.ok) {
      say(form, await problem(response, "Could not save the settings."));
      return;
    }
    await reloadSettings();
    say(one("[data-tuning]"), "Saved. Chat is judged by these from the next message on.");
  }

  // Replays stored chat under the settings as they are saved and as the
  // form has them, and puts what each came to in the table.
  async function trySettings(button) {
    const form = one("[data-tuning]");
    const chat = one("[data-trial-chat]");
    const url = chat ? `${form.dataset.trial}?chat=${encodeURIComponent(chat.value)}` : form.dataset.trial;
    button.disabled = true;
    say(button, "Replaying stored chat. This takes a few seconds.");
    const response = await post(url, { values: settingValues(form) });
    button.disabled = false;
    if (!response) {
      say(button, "Could not reach the service.");
      return;
    }
    if (!response.ok) {
      say(button, await problem(response, "Could not replay the chat."));
      return;
    }
    const result = await response.json();
    const table = one("[data-trial-result]");
    one("[data-trial-summary]", table).textContent = result.summary;
    one("[data-trial-rows]", table).replaceChildren(
      ...result.rows.map((row) => {
        const line = document.createElement("tr");
        for (const key of ["label", "moments", "per_hour", "not_extended", "median", "longest"]) {
          const cell = document.createElement(key === "label" ? "th" : "td");
          if (key === "label") cell.scope = "row";
          cell.textContent = row[key];
          line.append(cell);
        }
        return line;
      }),
    );
    table.hidden = false;
    say(button, "");
  }

  // ---- Health ----------------------------------------------------------
  //
  // The Health page says how long ago things happened, so what it says is
  // out of date within seconds. Like the other pages it does not reload:
  // every few seconds it is fetched again, and each part's words are put in
  // place of the ones on screen - with what stands beside Health in the bar.

  const HEALTH_POLL_MS = 5000;

  async function refreshHealth() {
    let fresh;
    try {
      const response = await fetch(location.href);
      if (!response.ok) return;
      fresh = new DOMParser().parseFromString(await response.text(), "text/html");
    } catch {
      return; // restarting or down - the page keeps what it says
    }
    for (const part of all("[data-health-part]")) {
      const now = one(`[data-health-part="${part.dataset.healthPart}"]`, fresh);
      if (now) part.replaceWith(now);
    }
    const flag = one("[data-health-flag]", fresh);
    if (flag) one("[data-health-flag]").textContent = flag.textContent;
  }

  // Not while the page is out of sight, and not under a selection: whoever
  // is copying an address out of it would lose their place.
  function watchHealth() {
    if (!document.hidden && getSelection().isCollapsed) refreshHealth();
  }

  async function checkSubscriptions(button) {
    button.disabled = true;
    say(button, "Asking Kick.");
    const response = await post("/subscriptions/check");
    button.disabled = false;
    if (!response) {
      say(button, "Could not reach the service.");
      return;
    }
    if (!response.ok) {
      say(button, await problem(response, "Could not check the subscriptions."));
      return;
    }
    await refreshHealth();
    say(button, "");
  }

  // ---- Wiring -----------------------------------------------------------

  const onClick = [
    ["a.ch-row", (row, event) => isPlainClick(event) && (event.preventDefault(), openMoment(row, { play: true }))],
    ["[data-rate]", (key) => rate(key.closest(".moment"), Number(key.dataset.rate))],
    ["[data-tag]", setTag],
    ["[data-trim-open]", openTrim],
    ["[data-trim-close]", (button) => trimOf(button).close()],
    ["[data-trim-play]", (button) => toggleTrimPlay(trimOf(button))],
    ["[data-trim-clip]", (video) => toggleTrimPlay(trimOf(video))],
    ["[data-trim-go]", exportTrim],
    ["[data-switch] button", flip],
    ["[data-setting] button", chooseSetting],
    ["[data-rest]", (rest) => (putBack(rest), settingsEdited(settingsOf(rest)))],
    [
      "[data-rest-all]",
      (button) => {
        const form = settingsOf(button);
        for (const rest of all("[data-rest]", form)) putBack(rest);
        settingsEdited(form);
      },
    ],
    ["[data-trial-go]", trySettings],
    ["[data-recording-start]", (button) => setRecording(button, true)],
    ["[data-recording-stop]", (button) => setRecording(button, false)],
    ["[data-delete-open]", (button) => askToDelete(button, true)],
    ["[data-delete-cancel]", (button) => askToDelete(button, false)],
    ["[data-delete-go]", deleteRecording],
    ["[data-save-again]", saveRecordingAgain],
    ["[data-subscriptions-check]", checkSubscriptions],
    ["[data-find-moments]", findMoments],
    ["[data-make-clips]", makeClips],
    ["[data-found-go]", playFound],
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
    const strip = event.target.closest("[data-strip], [data-rec-strip]");
    if (strip && !strip.contains(event.relatedTarget)) stopReadOut(strip);
  });

  // The trace under a recording is pressed and dragged along like the strip
  // under a clip.
  document.addEventListener("pointerdown", (event) => {
    const strip = event.target.closest("[data-rec-strip]");
    if (!strip || event.button !== 0) return;
    strip.setPointerCapture(event.pointerId);
    dragRecording(strip, event);
  });

  document.addEventListener("pointermove", (event) => {
    const strip = event.target.closest("[data-rec-strip]");
    if (!strip) return;
    if (strip.hasPointerCapture(event.pointerId)) dragRecording(strip, event);
    readOutRecording(strip, event);
  });

  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, (event) => {
      const strip = event.target.closest?.("[data-rec-strip]");
      if (strip && recordingDrag !== null) dragRecording(strip, event, { last: true });
    });
  }

  // In the Trim dialog: pressing a handle takes hold of it, pressing the
  // strip anywhere else goes there, and dragging does either along it.
  document.addEventListener("pointerdown", (event) => {
    const strip = event.target.closest("[data-trim-strip]");
    if (!strip || event.button !== 0) return;
    const dialog = trimOf(strip);
    const handle = event.target.closest("[data-trim-handle]");
    const time = trimPoint(strip, event);
    strip.setPointerCapture(event.pointerId);
    trimDrag = handle ? handle.dataset.trimHandle : null;
    // A handle is as wide as a finger, and is held wherever it was pressed.
    if (trimDrag) trimGrab = time - trimAt(dialog)[trimDrag];
    else seekTrim(dialog, time);
  });

  document.addEventListener("pointermove", (event) => {
    const strip = event.target.closest("[data-trim-strip]");
    if (!strip || !strip.hasPointerCapture(event.pointerId)) return;
    const time = trimPoint(strip, event);
    if (trimDrag) moveTrim(trimOf(strip), trimDrag, time - trimGrab);
    else seekTrim(trimOf(strip), time);
  });

  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => {
      const held = trimDrag;
      trimDrag = null;
      // A handle let go of at an end of a recording's window: the window
      // moves to where there is room to drag it further.
      const dialog = held && one("[data-trim][open]");
      if (dialog) fitTrimWindow(dialog);
    });
  }

  document.addEventListener("dblclick", (event) => {
    const video = event.target.closest("[data-clip]");
    if (video) toggleFullScreen(video);
    // Chat's slider rests in the middle, and goes back there when asked.
    const shift = event.target.closest("[data-trim-shift]");
    if (shift && !shift.disabled) {
      shift.value = "0";
      shiftTrimChat(shift);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    // With the Trim dialog open the keys are its own: a number must not
    // rate the moment underneath.
    const dialog = one("[data-trim][open]");
    if (dialog) {
      trimKey(dialog, event);
      return;
    }
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
    if (target.matches("[data-rec-strip]") && recordingKey(target, event)) return;
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
    } else if (target.matches("[data-trim-shift]")) {
      shiftTrimChat(target);
    } else if (target.matches("[data-chat-delay-slider]")) {
      previewChatShift(target);
    } else if (target.matches("[data-trim-background-colour]")) {
      paintTrimBackground(trimOf(target));
    } else if (target.matches("[data-setting]")) {
      settingsEdited(settingsOf(target));
    }
  });

  // A slider let go of with the mouse does not keep the focus, so Space goes
  // back to playing the clip.
  document.addEventListener("pointerup", (event) => {
    if (event.target.matches?.("[data-volume], [data-chat-delay-slider]")) event.target.blur();
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-note]")) saveNote(target);
    else if (target.matches("[data-title]")) saveTitle(target);
    else if (target.matches("[data-trim-type]")) typeTrim(target);
    else if (target.matches("[data-trim-chat]")) wantTrimChat(target);
    else if (target.matches("[data-trim-background]")) paintTrimBackground(trimOf(target));
    else if (target.matches("[data-chat-delay-slider]")) shiftChat(target);
    else if (target.matches("[data-submit-on-change]")) target.form.requestSubmit();
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-add-channel], [data-tuning]");
    if (!form) return;
    event.preventDefault();
    if (form.matches("[data-tuning]")) saveSettings(form);
    else addChannel(form);
  });

  // Media events do not bubble, so they are caught on the way down - and
  // neither does a dialog's closing.
  const onMedia = (selector, handle) => {
    for (const type of ["loadedmetadata", "durationchange", "timeupdate", "seeked", "play", "pause", "ended", "emptied", "volumechange", "ratechange"]) {
      document.addEventListener(type, (event) => event.target.matches?.(selector) && handle(event.target), true);
    }
  };
  onMedia("[data-clip]", paint);
  onMedia("[data-trim-clip]", trimTick);
  onMedia("[data-recording-clip]", paintRecording);
  document.addEventListener("close", (event) => event.target.matches?.("[data-trim]") && leaveTrim(event.target), true);

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
  one('.rec-row[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  if (one("[data-recording-clip]")) paintRecording(one("[data-recording-clip]"));
  loadRecordingTrace();
  watchFound();
  if (one('[data-recording-state="running"], [data-recording-state="saving"], [data-recording-row]')) watchRecordings();
  if (one("[data-always-record], [data-recordings]")) {
    setInterval(() => {
      if (recordingsWatch === null && recordingsAwaited()) checkRecordings();
    }, RECORDINGS_AWAITED_POLL_MS);
  }
  if (one("[data-health]")) setInterval(watchHealth, HEALTH_POLL_MS);
})();
