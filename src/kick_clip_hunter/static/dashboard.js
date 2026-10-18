// Clip Hunter dashboard.
//
// The pages are rendered on the server and work as plain pages. This script
// makes them quick to use: it opens a moment from the queue without loading
// the page again, saves a rating, tag or note as it is set, drives the clip
// player from the strip under it, replays chat in step with the clip, runs
// the review from the keyboard (1 to 5 rate, J and K move, Space plays, Del
// deletes), has the dialog a stretch of footage is cut out for an editor in
// - a moment's or a recording's - has a moment's transcript made, follows it
// as the clip plays and saves what is put right in it, saves the Settings
// page's form - whose sections are tabs, and whose lists are added to an
// item at a time - starts and stops the recording of a whole stream,
// replays a recording's chat beside it, puts what chat did over all of it
// under it, looks for moments in it, deletes moments and recordings and
// takes channels off the watchlist, and keeps the Health page saying what
// holds now.
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
  // A part can hold smaller parts with status lines of their own - a list
  // among the settings, the chat video under a recording - and its own line
  // is the one outside them.
  function say(from, words) {
    const scope = from.closest("[data-status-scope]");
    if (!scope) return;
    const lines = all("[data-status]", scope);
    const line = lines.find((status) => status.closest("[data-status-scope]") === scope) || lines[0];
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

  // Brings the transport, the playhead, the chat and the transcript in line
  // with the player.
  function paint(video) {
    const article = video.closest(".moment");
    if (!article) return;
    const span = spanOf(video);
    const time = clipTime(video);
    const words = timeWords(time, span);
    const length = span ? span.clip : Number.isFinite(video.duration) ? video.duration : 0;
    const playing = !video.paused && !video.ended;
    followTranscript(article, time);

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
  // chat if that is wanted: drawn into the picture, or as a video of its
  // own to lay over it. It has a player of its own on the same file and a
  // strip with a handle at either end of the stretch; either end can be
  // typed as a time as well. The cutting is done on the server and takes a
  // while with chat; the dialog asks now and then how far it has got, for
  // as long as it is open.
  //
  // A moment's dialog shows all of the moment's footage, on a copy of the
  // strip under the clip, and its times are clip time. A recording's is
  // "windowed": hours do not fit a strip a handle could be set on, so its
  // strip shows a few minutes of the file around the stretch - a window,
  // which follows the stretch wherever it is taken - and its times are
  // seconds of the file, written as the recording's player writes them.

  const TRIM_POLL_MS = 2000;
  const TRIM_BUSY = ["cutting", "chat", "burning"];
  // Where chat goes is kept from one cut to the next, and from one visit to
  // the next where the browser lets the page remember it.
  const TRIM_WHERE_KEY = "clip-hunter-chat-where";
  // So is where in the picture chat drawn into it goes, and how large.
  const TRIM_BOX_KEY = "clip-hunter-chat-box";
  // What a picture is taken to be until its video says.
  const TRIM_PICTURE = { width: 1920, height: 1080 };
  // Past its end by less than this, the player ran there; by more, it was put there.
  const TRIM_RAN_PAST_SECONDS = 1;
  // An end of the stretch closer than this share of the window's length to
  // an end of the window has no room left to be dragged: the window is set
  // around the stretch again.
  const TRIM_WINDOW_EDGE = 0.03;
  // A window this many times as long as the stretch would be given anew is
  // set anew: the stretch was made a good deal shorter.
  const TRIM_WINDOW_TOO_WIDE = 2;
  // An end of the stretch is taken hold of within this many pixels of it,
  // either side - a handle alone is narrow to hit.
  const TRIM_REACH_PIXELS = 10;
  // The stretch pressed between its ends moves once the pointer has moved
  // this many pixels; let go of before that, it was a press that goes there.
  const TRIM_STIR_PIXELS = 4;
  // What the dialog said of a time it could not read, while it says it.
  let trimNotATime = "";
  let trimWatch = null;
  // What is held on the strip: an end of the stretch ("from", "to"), the
  // whole stretch ("both"), or nothing - the strip, pressed to go there.
  let trimDrag = null;
  let trimHold = null; // the box of chat over the picture, while it is dragged
  let trimGrab = 0; // how far from the time it moves it was taken hold of
  let trimPress = null; // where the stretch was pressed, until it moves
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
      clip: windowed ? last : Number(dialog.dataset.clipSeconds),
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

  // Moves the whole stretch, as long as it is, to start at `from` - within
  // the footage - with the player on its start.
  function moveTrimStretch(dialog, time) {
    const span = trimSpan(dialog);
    const { from, to } = trimAt(dialog);
    const length = to - from;
    const start = clamp(time, span.first, Math.max(span.last - length, span.first));
    dialog.dataset.from = start.toFixed(2);
    dialog.dataset.to = (start + length).toFixed(2);
    const video = trimClip(dialog);
    video.pause();
    video.currentTime = start + span.lead;
    paintTrim(dialog);
  }

  // What pressing the strip at `event` takes hold of: a handle, or an end
  // of the stretch close to it, the stretch between its ends, or nothing.
  function trimHeld(strip, event) {
    const handle = event.target.closest("[data-trim-handle]");
    if (handle) return handle.dataset.trimHandle;
    const box = strip.getBoundingClientRect();
    const span = trimSpan(trimOf(strip));
    const x = (time) => box.left + ((time - span.start) / (span.end - span.start)) * box.width;
    const { from, to } = trimAt(trimOf(strip));
    const toFrom = Math.abs(event.clientX - x(from));
    const toTo = Math.abs(event.clientX - x(to));
    if (Math.min(toFrom, toTo) <= TRIM_REACH_PIXELS) return toFrom < toTo ? "from" : "to";
    return event.clientX > x(from) && event.clientX < x(to) ? "both" : null;
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
    hideCutLines(pane);
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
      dialog.dataset.clipSeconds = trace.dataset.clipSeconds;
      const copies = (parts) => parts.map((part) => part.cloneNode(true));
      const drawn = all(":scope > *", strip).filter((part) => !part.matches("[data-hover], [data-playhead], [data-flag], .ch-note"));
      one("[data-trim-drawing]", dialog).replaceChildren(...copies(drawn));
      one("[data-trim-axis]", dialog).replaceChildren(...copies(all("[data-trace] .ch-axis > *", article)));
      one("[data-trim-lines]", dialog)?.replaceChildren(...copies(all("[data-chat] > [data-at]", article)));
    }

    const where = one("[data-trim-where]", dialog);
    if (where) {
      try {
        const kept = localStorage.getItem(TRIM_WHERE_KEY);
        if (Array.from(where.options).some((option) => option.value === kept)) where.value = kept;
        if (dialog.dataset.box === undefined) {
          const box = JSON.parse(localStorage.getItem(TRIM_BOX_KEY) || "null");
          if (isTrimBox(box)) dialog.dataset.box = JSON.stringify(box);
        }
      } catch {
        // no storage here: the first choices stand
      }
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
    placeTrimChat(dialog);
    if (windowed) fitTrimWindow(dialog, { anew: true });
    else paintTrim(dialog);
    askTrim(dialog);
  }

  function leaveTrim(dialog) {
    trimClip(dialog).pause();
    clearTimeout(trimWatch);
    // The cut it made has subtitles of its own beside the clip.
    if (dialog.closest(".moment") && one("[data-transcript] [data-transcript-lines]")) refreshTranscript();
  }

  // Whether the chat goes with the video: its lines are then shown beside
  // the picture, and where it goes can be chosen, and it can be moved
  // against the picture and given a background.
  function wantTrimChat(box) {
    const dialog = trimOf(box);
    one("[data-trim-chat-pane]", dialog).hidden = !box.checked;
    for (const option of ["[data-trim-where]", "[data-trim-shift]", "[data-trim-background]"]) {
      one(option, dialog).disabled = !box.checked;
    }
    paintTrimBackground(dialog);
    placeTrimChat(dialog);
    paintTrim(dialog);
  }

  // -- chat drawn into the picture: a box over it, dragged to where chat
  // goes and by a corner to how large it is. Its width says how large the
  // text is, its height how much of the column shows - in shares of the
  // picture, kept to what the service keeps chat drawn into a picture to
  // (chat_video.placement), whose numbers the dialog carries.

  function trimColumn(dialog) {
    return JSON.parse(dialog.dataset.chatColumn);
  }

  function trimPicture(dialog) {
    const video = trimClip(dialog);
    return video.videoWidth ? { width: video.videoWidth, height: video.videoHeight } : TRIM_PICTURE;
  }

  function isTrimBox(box) {
    return Boolean(box) && ["left", "top", "width", "height"].every((side) => Number.isFinite(box[side]));
  }

  // Where chat goes when nothing else was said: as the service puts it.
  function firstTrimBox(dialog) {
    const column = trimColumn(dialog);
    const picture = trimPicture(dialog);
    const scale = Math.min(picture.width, picture.height) / 1080;
    const [across, down, margin] = [column.width, column.height, column.margin].map((pixels) => pixels * scale);
    return {
      left: (picture.width - across - margin) / picture.width,
      top: (picture.height - down - margin) / picture.height,
      width: across / picture.width,
      height: down / picture.height,
    };
  }

  // A box as chat can be drawn: its text no smaller or larger than it may
  // be, a few lines of it at least, within the picture. `anchor` is the
  // corner that stays where it is while another is dragged.
  function fitTrimBox(dialog, box, anchor = null) {
    const column = trimColumn(dialog);
    const picture = trimPicture(dialog);
    const share = column.width / picture.width;
    const width = clamp(box.width, column.scale_least * share, Math.min(column.scale_most * share, 1));
    const scale = (width * picture.width) / column.width;
    const height = clamp(box.height, Math.min((column.least * scale) / picture.height, 1), 1);
    let { left, top } = box;
    if (anchor) {
      left = anchor.west ? anchor.x : anchor.x - width;
      top = anchor.north ? anchor.y : anchor.y - height;
    }
    return { left: clamp(left, 0, 1 - width), top: clamp(top, 0, 1 - height), width, height };
  }

  function trimBox(dialog) {
    const kept = dialog.dataset.box && JSON.parse(dialog.dataset.box);
    return fitTrimBox(dialog, isTrimBox(kept) ? kept : firstTrimBox(dialog));
  }

  // Where the picture is drawn in the player, which keeps its shape.
  function trimShown(dialog) {
    const frame = trimClip(dialog).getBoundingClientRect();
    const picture = trimPicture(dialog);
    const fit = Math.min(frame.width / picture.width, frame.height / picture.height);
    const width = picture.width * fit;
    const height = picture.height * fit;
    return { x: (frame.width - width) / 2, y: (frame.height - height) / 2, width, height };
  }

  // Shows chat over the picture where it is drawn into it - the chat beside
  // the player moved into the box, as large as it will be - or beside the
  // player again for a video of its own.
  function placeTrimChat(dialog) {
    const overlay = one("[data-trim-overlay]", dialog);
    if (!overlay) return;
    const pane = one("[data-trim-chat-pane]", dialog);
    const inside = one("[data-trim-chat]", dialog).checked && one("[data-trim-where]", dialog).value === "video";
    overlay.hidden = !inside;
    if (!inside) {
      if (pane.parentElement === overlay) one(".trim-view", dialog).append(pane);
      for (const property of ["--chat-scale", "height"]) pane.style.removeProperty(property);
      hideCutLines(pane);
      return;
    }
    if (pane.parentElement !== overlay) overlay.prepend(pane);
    const box = trimBox(dialog);
    const shown = trimShown(dialog);
    const column = trimColumn(dialog);
    Object.assign(overlay.style, {
      left: `${shown.x + box.left * shown.width}px`,
      top: `${shown.y + box.top * shown.height}px`,
      width: `${box.width * shown.width}px`,
      height: `${box.height * shown.height}px`,
    });
    // The column as the page chat is laid out on has it, made as large as
    // the box: its pixels to one of the box's.
    const scale = (box.width * shown.width) / column.width;
    const height = (box.height * shown.height) / scale;
    pane.style.setProperty("--chat-scale", String(scale));
    pane.style.height = `${height}px`;
    hideCutLines(pane);
  }

  // In the box over the picture, the oldest line shown is whole or not
  // there, as the video will have it: a line the box's top would cut is
  // hidden - unless it is the newest, taller than the box by itself. Beside
  // the player every line shows, as chat always does there.
  function hideCutLines(pane) {
    const box = one(".chat-lines", pane);
    if (!box) return;
    const inside = Boolean(pane.closest("[data-trim-overlay]"));
    const edge = box.getBoundingClientRect().top;
    let newest = true;
    let cut = false;
    for (const line of Array.from(box.children).reverse()) {
      if (line.hidden) continue;
      if (inside && !cut && !newest) cut = line.getBoundingClientRect().top < edge - 0.5;
      newest = false;
      line.style.visibility = cut ? "hidden" : "";
    }
  }

  function holdTrimBox(overlay, event) {
    const dialog = trimOf(overlay);
    const corner = event.target.closest("[data-trim-corner]")?.dataset.trimCorner ?? null;
    overlay.setPointerCapture(event.pointerId);
    trimHold = { dialog, corner, x: event.clientX, y: event.clientY, box: trimBox(dialog), shown: trimShown(dialog) };
  }

  function dragTrimBox(event) {
    const { dialog, corner, box, shown } = trimHold;
    const dx = (event.clientX - trimHold.x) / shown.width;
    const dy = (event.clientY - trimHold.y) / shown.height;
    let moved;
    if (!corner) {
      moved = fitTrimBox(dialog, { ...box, left: box.left + dx, top: box.top + dy });
    } else {
      // The corner held follows the pointer; the one across from it stays.
      const west = corner.includes("w");
      const north = corner.includes("n");
      const anchor = {
        x: west ? box.left + box.width : box.left,
        y: north ? box.top + box.height : box.top,
        west: !west,
        north: !north,
      };
      const x = clamp((west ? box.left : box.left + box.width) + dx, 0, 1);
      const y = clamp((north ? box.top : box.top + box.height) + dy, 0, 1);
      moved = fitTrimBox(dialog, { ...box, width: Math.abs(anchor.x - x), height: Math.abs(anchor.y - y) }, anchor);
    }
    dialog.dataset.box = JSON.stringify(moved);
    placeTrimChat(dialog);
    paintTrim(dialog);
  }

  function letGoOfTrimBox() {
    keepTrimChoice(TRIM_BOX_KEY, trimHold.dialog.dataset.box ?? "");
    trimHold = null;
  }

  function keepTrimChoice(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // kept for this page only, then
    }
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
    const cut = one("[data-trim-cut]", dialog);
    cut.hidden = !trim.cut;
    if (trim.cut) {
      const span = trimSpan(dialog);
      cut.textContent = `The video holds ${trimWords(trim.cut.from, span)} to ${trimWords(trim.cut.to, span)} (${(trim.cut.to - trim.cut.from).toFixed(1)} s).`;
    }
    const files = [
      ["[data-trim-video]", trim.video_url],
      ["[data-trim-chat-video]", trim.chat_url],
      ["[data-trim-subtitles]", trim.subtitles_url],
    ];
    for (const [part, url] of files) {
      const link = one(part, dialog);
      link.hidden = !url;
      if (url) link.href = url;
      else link.removeAttribute("href");
    }
    one("[data-trim-go]", dialog).disabled = busy;
    const later = one("[data-trim-chat-later]", dialog);
    if (later) {
      later.hidden = !trim.chat_later;
      later.disabled = busy;
    }
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
      chat_where: chat ? one("[data-trim-where]", dialog).value : null,
      chat_shift: shift ? Number(shift.value) : 0,
      chat_background: chat ? trimBackground(dialog) : null,
      chat_box: chat && one("[data-trim-where]", dialog).value === "video" ? trimBox(dialog) : null,
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

  // The chat of the video that is ready, made after it: for the stretch
  // that video holds, as the options have it now - drawn into it, or as a
  // video of its own.
  async function trimChatLater(button) {
    const dialog = trimOf(button);
    const shift = one("[data-trim-shift]", dialog);
    button.disabled = true;
    const response = await post(`${dialog.dataset.trim}/chat`, {
      chat_where: one("[data-trim-where]", dialog).value,
      chat_shift: Number(shift.value),
      chat_background: trimBackground(dialog),
      chat_box: one("[data-trim-where]", dialog).value === "video" ? trimBox(dialog) : null,
    });
    if (!dialog.isConnected) return;
    if (!response || !response.ok) {
      button.disabled = false;
      one("[data-trim-state]", dialog).textContent = response
        ? await problem(response, "Could not start making the chat. Try again.")
        : "Could not reach the service.";
      return;
    }
    showTrim(dialog, await response.json());
  }

  // A moment something was downloaded from is marked so in the queue; the
  // service notes it as it hands the file over.
  function markTaken(link) {
    const article = link.closest(".moment");
    const row = article && one(`a.ch-row[data-moment="${article.dataset.moment}"] [data-taken]`);
    if (row) row.hidden = false;
  }

  // ---- What is said in the clip -----------------------------------------
  //
  // Beside a clip, on a tab next to its chat, is its transcript: what is
  // said in its footage, line by line with its times. The service writes it
  // down when asked, which takes it a while, so the tab is fetched again
  // every couple of seconds until it is there. A line's time plays the clip
  // from there, the line being said is marked as the clip plays, and what
  // is put right in a line is saved as the line is left. The subtitles made
  // of it are downloaded from the tab - and from the Trim dialog, beside
  // the cut they are timed to.

  const TRANSCRIPT_POLL_MS = 2000;
  let transcriptWatch = null;
  // The tab shown beside a clip, kept from one moment to the next.
  let side = "chat";

  // A line is one row until more is in it - which can only be measured
  // while it is on screen.
  function growLines(root) {
    for (const field of all("[data-transcript-line]", root)) {
      if (!field.closest("[hidden]")) grow(field);
    }
  }

  function showSide(article, which) {
    const tabs = all("[data-side]", article);
    if (!tabs.length) return;
    side = which;
    for (const tab of tabs) tab.setAttribute("aria-selected", String(tab.dataset.side === which));
    for (const pane of all("[data-side-pane]", article)) pane.hidden = pane.dataset.sidePane !== which;
    growLines(article);
    // Chat that arrived while it was out of sight is at the bottom.
    const box = one("[data-chat]", article);
    if (which === "chat" && box) box.scrollTop = box.scrollHeight;
  }

  function chooseSide(tab) {
    const article = tab.closest(".moment");
    showSide(article, tab.dataset.side);
    const video = one("[data-clip]", article);
    if (video) paint(video);
  }

  // Marks the line of the transcript being said at a clip time and keeps it
  // in sight - unless a line is being put right, which stays where it is.
  function followTranscript(article, time) {
    const list = one("[data-transcript-lines]", article);
    if (!list) return;
    let now = null;
    for (const line of list.children) {
      const said = Number(line.dataset.from) <= time && time < Number(line.dataset.to);
      line.classList.toggle("is-now", said);
      if (said) now = line;
    }
    if (!now || list.closest("[hidden]") || list.contains(document.activeElement)) return;
    if (now.offsetTop < list.scrollTop || now.offsetTop + now.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = now.offsetTop - list.clientHeight / 3;
    }
  }

  function playLine(button) {
    const video = clipOf(button);
    if (video) goTo(video, Number(button.closest("[data-from]").dataset.from), { play: true });
  }

  // The tab as the service has it now, in place of the one on the page.
  async function refreshTranscript() {
    const current = one("[data-transcript]");
    if (!current) return;
    let markup = null;
    try {
      const response = await fetch(current.dataset.page);
      if (response.ok) markup = await response.text();
    } catch {
      // what is on the page stays, and is asked about again
    }
    const fresh = markup && one("[data-transcript]", new DOMParser().parseFromString(markup, "text/html"));
    const now = one("[data-transcript]");
    // Another moment may be open by the time the answer is here; and a
    // line that is being put right is not taken away from under the cursor.
    if (!fresh || !now || now.dataset.page !== fresh.dataset.page || now.contains(document.activeElement)) return;
    now.replaceWith(fresh);
    growLines(fresh);
    const video = clipOf(fresh);
    if (video) paint(video);
    watchTranscript();
  }

  function watchTranscript() {
    const busy = Boolean(one("[data-transcript][data-busy]"));
    if (busy && transcriptWatch === null) {
      transcriptWatch = setInterval(refreshTranscript, TRANSCRIPT_POLL_MS);
    } else if (!busy && transcriptWatch !== null) {
      clearInterval(transcriptWatch);
      transcriptWatch = null;
    }
  }

  async function transcribe(button) {
    const pane = button.closest("[data-transcript]");
    button.disabled = true;
    const response = await post(pane.dataset.transcript);
    if (!pane.isConnected) return; // another moment was opened meanwhile
    if (!response || !response.ok) {
      button.disabled = false;
      say(button, response ? await problem(response, "Could not start the transcript. Try again.") : "Could not reach the service.");
      return;
    }
    await refreshTranscript();
  }

  // Saves what a line of the transcript says, if that was changed since it
  // was last saved: when the line is left, and before the page goes.
  async function correctLine(field) {
    if (!field?.matches?.("[data-transcript-line]") || field.value === field.defaultValue) return;
    const pane = field.closest("[data-transcript]");
    const text = field.value;
    const response = await post(`${pane.dataset.transcript}/${field.dataset.transcriptLine}`, { text });
    if (!field.isConnected) return;
    if (!response || !response.ok) {
      say(field, response ? await problem(response, "Could not save the line. Try again.") : "Could not reach the service. The line was not saved.");
      return;
    }
    const saved = (await response.json()).text;
    field.defaultValue = saved;
    // As the service tidied it - unless it is being typed into again.
    if (document.activeElement !== field && field.value === text) {
      field.value = saved;
      grow(field);
    }
    say(field, "Line saved.");
  }

  // Enter goes on to the next line (Shift+Enter starts a second row of this
  // one, for a subtitle of two rows) and Escape leaves the line; leaving a
  // line is what saves it.
  function transcriptKey(field, event) {
    if (event.key === "Escape") {
      event.preventDefault();
      field.blur();
    } else if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const next = field.closest("li").nextElementSibling;
      const following = next && one("[data-transcript-line]", next);
      if (following) following.focus();
      else field.blur();
    }
  }

  // Fits what the script looks after to markup that has just arrived.
  function dress() {
    for (const field of all("[data-note]")) grow(field);
    for (const button of all("[data-trim-open], [data-find-moments], [data-make-chat-file]")) button.hidden = false;
    markFound();
    const article = one(".moment");
    if (article) showSide(article, side);
    watchTranscript();
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
      say(form, `No channel called ${name}. Check the spelling.`);
    } else if (response && response.status === 503) {
      // What the service lacks to watch a channel of that platform.
      say(form, (await response.json()).detail || `Could not add ${name}.`);
    } else {
      say(form, `Could not add ${name}. The server log says why.`);
    }
  }

  // Shutting down is asked for twice: the button in the bar, then the same
  // word again in a dialog, next to a sentence that says what it will do.
  function askToShutDown(asking) {
    const dialog = one("[data-shutdown]");
    if (!asking) {
      dialog.close();
      return;
    }
    dialog.showModal();
    // Cancel takes the focus, so a stray Enter or Space backs out.
    one("[data-shutdown-cancel]", dialog).focus();
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
    const opener = one("[data-shutdown-open]");
    opener.disabled = true;
    opener.textContent = "Shutting down";
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
    watchChatFile();
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

  // A recording or a moment: what to go to once it is gone is the page the
  // group names, or for a moment the next one in the queue.
  async function deleteIt(button) {
    const group = button.closest("[data-delete]");
    const then = group.dataset.deleteThen;
    button.disabled = true;
    // A file that is being played is held open, and can't be removed - by
    // the player on the page, or by the one in its Trim dialog.
    for (const video of all("video", button.closest("article") || document)) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    const response = await post(button.dataset.deleteGo);
    if (response && response.ok) {
      if (then === "moment") removeMoment(button.closest(".moment"));
      else location.assign(then);
      return;
    }
    button.disabled = false;
    say(button, response ? await problem(response, "Could not delete it.") : "Could not reach the service.");
  }

  // The deleted moment's row goes from the queue, the counts on its tabs
  // and the ones new moments are told by go down, and the next moment opens
  // - or, with none left on the page, the list is fetched again.
  function removeMoment(article) {
    const row = one(`a.ch-row[data-moment="${article.dataset.moment}"]`);
    const list = rows();
    const index = list.indexOf(row);
    const next = index < 0 ? null : list[index + 1] || list[index - 1] || null;

    const queue = one("[data-queue]");
    if (queue && !(queue.dataset.channel && queue.dataset.channel !== article.dataset.channel)) {
      const rating = Number(article.dataset.rating);
      const bump = (show, by) => {
        const count = one(`[data-show="${show}"] [data-count]`, queue);
        if (count && by) count.textContent = String(Math.max(0, Number(count.textContent) - by));
      };
      bump("all", 1);
      bump("unrated", Number(rating === 0));
      bump("best", Number(rating >= Number(queue.dataset.bestFrom)));
    }
    const news = one("[data-news]");
    if (news) {
      news.dataset.moments = String(Number(news.dataset.moments) - 1);
      if (one("[data-clip]", article)) news.dataset.clips = String(Number(news.dataset.clips) - 1);
    }

    if (row) {
      const group = row.closest(".queue-group");
      row.remove();
      if (group && !one("a.ch-row", group)) group.remove();
    }
    if (next) {
      openMoment(next);
      return;
    }
    const url = new URL(location.href);
    url.searchParams.delete("moment");
    location.assign(url);
  }

  // Del asks to delete the open moment, and Del again deletes it; Escape
  // backs out.
  function deleteKey(key) {
    const group = one(".moment [data-delete]");
    if (!group || loading) return false;
    const asking = !one("[data-delete-confirm]", group).hidden;
    if (key === "delete") {
      if (asking) deleteIt(one("[data-delete-go]", group));
      else askToDelete(group, true);
      return true;
    }
    if (key === "escape" && asking) {
      askToDelete(group, false);
      return true;
    }
    return false;
  }

  // Removing a channel from the watchlist is asked for in a dialog, which
  // also offers to delete what came of watching it.
  function askToRemoveChannel(button) {
    const dialog = one("[data-remove-channel-dialog]");
    dialog.dataset.slug = button.dataset.removeChannel;
    one("[data-remove-channel-name]", dialog).textContent = button.dataset.removeChannel;
    one("[data-remove-channel-kept]", dialog).textContent = button.dataset.kept;
    one("[data-remove-everything]", dialog).checked = false;
    one("[data-remove-channel-go]", dialog).disabled = false;
    say(dialog, "");
    dialog.showModal();
    // Cancel takes the focus, so a stray Enter or Space backs out.
    one("[data-remove-channel-cancel]", dialog).focus();
  }

  async function removeChannel(button) {
    const dialog = button.closest("[data-remove-channel-dialog]");
    const everything = one("[data-remove-everything]", dialog).checked;
    button.disabled = true;
    say(dialog, everything ? "Deleting. A channel with a lot of chat takes a while." : "");
    const slug = encodeURIComponent(dialog.dataset.slug);
    const response = await post(`/channels/${slug}/delete?everything=${everything ? 1 : 0}`);
    if (response && response.ok) {
      location.reload();
      return;
    }
    button.disabled = false;
    say(dialog, response ? await problem(response, "Could not remove the channel.") : "Could not reach the service.");
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

  // ---- The chat of a whole recording ------------------------------------

  // Download chat, beside a recording's Download: its chat as a video of
  // its own, made the first time it is asked for. That takes minutes, so
  // the button is fetched again every couple of seconds while it is made,
  // and the video is downloaded once it is there - if it was asked for on
  // this page.

  const CHAT_FILE_POLL_MS = 2000;
  let chatFileWatch = null;
  let chatFileAsked = null;

  async function refreshChatFile() {
    const current = one("[data-rec-chat-file]");
    if (!current) return;
    let markup = null;
    try {
      const response = await fetch(current.dataset.page);
      if (response.ok) markup = await response.text();
    } catch {
      // what is on the page stays, and is asked about again
    }
    const fresh = markup && one("[data-rec-chat-file]", new DOMParser().parseFromString(markup, "text/html"));
    const now = one("[data-rec-chat-file]");
    // Another recording may be open by the time the answer is here.
    if (!fresh || !now || now.dataset.url !== fresh.dataset.url) return;
    now.replaceWith(fresh);
    dress();
    const ready = one("[data-chat-file-ready]", fresh);
    if (ready && chatFileAsked === fresh.dataset.url) {
      chatFileAsked = null;
      ready.click();
    }
    watchChatFile();
  }

  function watchChatFile() {
    const busy = Boolean(one("[data-rec-chat-file][data-busy]"));
    if (busy && chatFileWatch === null) {
      chatFileWatch = setInterval(refreshChatFile, CHAT_FILE_POLL_MS);
    } else if (!busy && chatFileWatch !== null) {
      clearInterval(chatFileWatch);
      chatFileWatch = null;
    }
  }

  async function makeChatFile(button) {
    const place = button.closest("[data-rec-chat-file]");
    button.disabled = true;
    const response = await post(place.dataset.url);
    if (!response || !response.ok) {
      button.disabled = false;
      say(button, response ? await problem(response, "Could not make the chat video.") : "Could not reach the service.");
      return;
    }
    chatFileAsked = place.dataset.url;
    await refreshChatFile();
  }

  // ---- Settings ---------------------------------------------------------

  // The Settings page is one form, saved as a whole, whose sections are
  // tabs: one is shown at a time, and the others stay in the form as they
  // were left, so that what was set in each is saved together. Each setting
  // has a button beside it that puts back what it would be if it were left
  // alone (what is recommended, or for a channel what all channels have),
  // and what is in the form can be tried on stored chat before it is saved.
  // A list is what it holds, a rectangle an item: an item is added from the
  // field beside it, one at a time, and taken out by its cross.

  const settingsOf = (control) => control.closest("[data-tuning]");
  const isList = (field) => field.dataset.kind === "words" || field.dataset.kind === "phrases";
  // What a list holds, in the order the service keeps it in.
  const listed = (list) => all("[data-item]", list).map((item) => item.dataset.item);
  const sectionTab = (inside) => one(`[data-section="${inside.closest("[data-section-pane]").dataset.sectionPane}"]`);

  // What a setting's field says, the way the service takes it. A number
  // typed with a comma is the same number; one that is no number at all is
  // sent as nothing, and the service says what the field takes.
  function settingValue(field) {
    if (field.dataset.kind === "switch") return one('[aria-pressed="true"]', field).dataset.enabled === "1";
    if (isList(field)) return listed(field);
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

  // Marks the settings that are not what they would be if left alone, lets
  // those be put back, and has the tab of each section say how many of its
  // own there are - the sections that are not shown included.
  function markSettings(form) {
    for (const field of all("[data-setting]", form)) {
      const row = field.closest(".set-row");
      const rest = one("[data-rest]", row);
      const value = settingValue(field);
      let atRest;
      if (field.dataset.kind === "switch") atRest = value === (rest.dataset.rest === "1");
      else if (isList(field)) atRest = JSON.stringify([...value].sort()) === JSON.stringify(JSON.parse(rest.dataset.rest).sort());
      else atRest = value === Number(rest.dataset.rest);
      row.classList.toggle("is-set", !atRest);
      rest.disabled = atRest;
    }
    for (const pane of all("fieldset[data-section-pane]", form)) {
      const count = all(".set-row.is-set", pane).length;
      const words = one("[data-set-count]", sectionTab(pane));
      if (words) words.textContent = count ? `${count} set` : "";
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
    } else if (isList(field)) {
      holdItems(field, JSON.parse(rest.dataset.rest));
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

  // Shows one section of the settings, its tab marked as where you are. The
  // address says which, so the page fetched again after a save opens on it
  // too - and so do another channel's settings, chosen on the left.
  function showSection(tab, { focus = false } = {}) {
    const key = tab.dataset.section;
    for (const other of all("[data-section]")) {
      other.setAttribute("aria-selected", String(other === tab));
      other.tabIndex = other === tab ? 0 : -1;
    }
    for (const pane of all("[data-section-pane]")) pane.hidden = pane.dataset.sectionPane !== key;
    const panes = one(".set-panes");
    if (panes) panes.scrollTop = 0;
    one("[data-settings]").dataset.shown = key;
    const kept = one("[data-section-kept]");
    if (kept) kept.value = key;
    const address = new URL(location.href);
    address.searchParams.set("section", key);
    history.replaceState(null, "", address);
    if (focus) tab.focus();
  }

  // On the keyboard the tabs are one stop: the arrows go from one to the
  // next, Home and End to the first and the last.
  function sectionKey(tab, event) {
    const tabs = all("[data-section]");
    const at = tabs.indexOf(tab);
    const steps = { ArrowDown: at + 1, ArrowRight: at + 1, ArrowUp: at - 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 };
    if (!(event.key in steps)) return false;
    event.preventDefault();
    showSection(tabs[(steps[event.key] + tabs.length) % tabs.length], { focus: true });
    return true;
  }

  // A list's rectangles, made anew for what it is to hold and in the order
  // the service keeps it in - so an item added is where it will stay.
  function holdItems(list, items) {
    const copy = one("[data-chip-template]").content.firstElementChild;
    list.replaceChildren(
      ...[...items].sort().map((item) => {
        const made = copy.cloneNode(true);
        made.dataset.item = item;
        one("span", made).textContent = item;
        one("[data-chip-remove]", made).setAttribute("aria-label", `Remove ${item}`);
        return made;
      }),
    );
  }

  // Adds what was typed beside a list to it, the way the service will keep
  // it, and empties the field. What the service would not take is said
  // beside the field instead, and stays in it to be put right. Whether the
  // field is empty now.
  function addTyped(input) {
    const row = input.closest(".set-row");
    const list = one("[data-setting]", row);
    const item = input.value.trim().toLowerCase();
    if (!item) return true;
    const items = listed(list);
    const shortest = Number(list.dataset.shortest);
    const most = Number(list.dataset.mostItems);
    let wrong = "";
    if (list.dataset.kind === "words" && /\s/.test(item)) {
      wrong = `'${item}' has to be one word - no emote's name has a space in it.`;
    } else if ([...item].length < shortest) {
      wrong = `'${item}' is too short: it has to be ${shortest} characters long at the least.`;
    } else if (!items.includes(item) && items.length >= most) {
      wrong = `"${one(".set-name", row).textContent.trim()}" can hold ${most} at the most.`;
    }
    if (wrong) {
      say(input, wrong);
      return false;
    }
    input.value = "";
    if (items.includes(item)) {
      say(input, `'${item}' is in the list already.`);
      return true;
    }
    holdItems(list, [...items, item]);
    say(input, "");
    settingsEdited(settingsOf(input));
    return true;
  }

  function addFromButton(button) {
    const input = one("[data-chip-input]", button.closest(".set-row"));
    addTyped(input);
    input.focus();
  }

  // Takes an item out of its list. From the keyboard the focus goes on to
  // the cross of the item that takes its place, or back to the field once
  // there is none.
  function removeItem(cross, event) {
    const item = cross.closest("[data-item]");
    const row = item.closest(".set-row");
    const form = settingsOf(item);
    const next = item.nextElementSibling || item.previousElementSibling;
    item.remove();
    if (event.detail === 0) (next ? one("[data-chip-remove]", next) : one("[data-chip-input]", row)).focus();
    settingsEdited(form);
  }

  // What was typed beside a list and not added is added before the form is
  // saved or tried: it is what was meant. Whether all of it could be; where
  // something could not, its section is shown with the field in focus, and
  // beside it it says why.
  function addAllTyped(form) {
    let fine = true;
    for (const input of all("[data-chip-input]", form)) {
      if (input.closest("fieldset:disabled") || addTyped(input)) continue;
      if (fine) {
        showSection(sectionTab(input));
        input.focus();
      }
      fine = false;
    }
    return fine;
  }

  // The service names a setting it refuses by what the page calls it, in
  // quotes - one that may be in a section that is not shown, or folded
  // away. The first it names is shown.
  function showRefused(form, words) {
    let first = null;
    for (const name of all(".set-name", form)) {
      const at = words.indexOf(`"${name.textContent.trim()}"`);
      if (at >= 0 && (first === null || at < first.at)) first = { at, name };
    }
    if (!first) return;
    const more = first.name.closest("details");
    if (more) more.open = true;
    if (first.name.closest("[hidden]")) showSection(sectionTab(first.name));
    first.name.scrollIntoView({ block: "nearest" });
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
    const scrolled = one(".set-panes", page)?.scrollTop ?? 0;
    page.replaceWith(fresh);
    const panes = one(".set-panes", fresh);
    if (panes) panes.scrollTop = scrolled;
  }

  async function saveSettings(form) {
    if (!addAllTyped(form)) {
      say(form, "Not saved: something typed beside a list could not be added to it.");
      return;
    }
    say(form, "Saving.");
    const response = await post(form.dataset.tuning, { values: settingValues(form) });
    if (!response) {
      say(form, "Could not reach the service. Nothing was saved.");
      return;
    }
    if (!response.ok) {
      const words = await problem(response, "Could not save the settings.");
      say(form, words);
      showRefused(form, words);
      return;
    }
    await reloadSettings();
    say(one("[data-tuning]"), "Saved. Chat is judged by these from the next message on.");
  }

  // Replays stored chat under the settings as they are saved and as the
  // form has them, and puts what each came to in the table.
  async function trySettings(button) {
    const form = one("[data-tuning]");
    if (!addAllTyped(form)) {
      say(form, "Not tried: something typed beside a list could not be added to it.");
      return;
    }
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

  // ---- Notifications, on the Settings page ------------------------------
  //
  // Where notifications go and which kinds go out are the service's, not
  // detection's: saved together by the tab's own button, and never sent
  // with the form they stand in. A secret - the bot's token, the webhook -
  // is never shown: its field stays empty, and left empty what is set stays
  // set. Find chats asks Telegram which chats wrote to the bot, to pick the
  // one notes go to.

  const notifyPane = (inside) => inside.closest("[data-notify-setup]");
  const notifyField = (pane, name) => one(`[data-notify-field="${name}"]`, pane);

  function notifyValues(pane) {
    const setup = {};
    for (const field of all("[data-notify-field]", pane)) {
      const value = field.value.trim();
      if (!("secret" in field.dataset)) setup[field.dataset.notifyField] = value;
      else if (value) setup[field.dataset.notifyField] = value;
      else if (field.dataset.remove === "1") setup[field.dataset.notifyField] = "";
    }
    const kinds = Object.fromEntries(
      all("[data-notify-kind]", pane).map((kind) => [kind.dataset.notifyKind, one('[aria-pressed="true"]', kind).dataset.enabled === "1"]),
    );
    return { setup, kinds };
  }

  async function saveNotifications(control) {
    const pane = notifyPane(control);
    say(pane, "Saving.");
    const response = await post("/notifications/setup", notifyValues(pane));
    if (!response) {
      say(pane, "Could not reach the service. Nothing was saved.");
      return;
    }
    if (!response.ok) {
      say(pane, await problem(response, "Could not save where notifications go."));
      return;
    }
    await reloadSettings();
    const fresh = one("[data-notify-setup]");
    if (fresh) say(fresh, "Saved. The next notification goes by these.");
  }

  function chooseKind(half) {
    for (const other of all("button", half.closest("[data-notify-kind]"))) other.setAttribute("aria-pressed", String(other === half));
    say(notifyPane(half), "Not saved yet.");
  }

  // A secret that is set is taken away once the tab is saved.
  function removeSecret(button) {
    const field = one("[data-notify-field]", button.closest(".set-row"));
    field.value = "";
    field.dataset.remove = "1";
    field.placeholder = "Removed once saved";
    button.disabled = true;
    say(notifyPane(button), "Not saved yet.");
  }

  // The bot is the one whose token is typed in, or else the one that is set.
  async function findChats(button) {
    const pane = notifyPane(button);
    const list = one("[data-found-chats]", button.closest(".set-row"));
    button.disabled = true;
    say(button, "Asking Telegram.");
    const response = await post("/notifications/telegram/chats", {
      token: notifyField(pane, "telegram_bot_token").value.trim(),
      api_url: notifyField(pane, "telegram_api_url").value.trim(),
    });
    button.disabled = false;
    if (!response) {
      say(button, "Could not reach the service.");
      return;
    }
    if (!response.ok) {
      say(button, await problem(response, "Could not ask Telegram."));
      return;
    }
    const { bot, chats } = await response.json();
    list.replaceChildren(
      ...chats.map((chat) => {
        const item = document.createElement("li");
        const pick = document.createElement("button");
        pick.type = "button";
        pick.className = "set-chip set-pick";
        pick.dataset.chatPick = chat.id;
        pick.textContent = `${chat.name || "A chat"} (${chat.kind}, ${chat.id})`;
        item.append(pick);
        return item;
      }),
    );
    say(
      button,
      chats.length
        ? `These wrote to @${bot} lately. Pick the one notes go to.`
        : `No chat wrote to @${bot} lately. Write to it from the chat notes are to go to, or add it to a group, and look again.`,
    );
  }

  function pickChat(button) {
    const pane = notifyPane(button);
    notifyField(pane, "telegram_chat_id").value = button.dataset.chatPick;
    say(button, "");
    say(pane, "Not saved yet.");
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

  async function testNotifications(button) {
    button.disabled = true;
    say(button, "Sending.");
    const response = await post("/notifications/test");
    button.disabled = false;
    if (!response) {
      say(button, "Could not reach the service.");
      return;
    }
    if (!response.ok) {
      say(button, await problem(response, "Could not send a test."));
      return;
    }
    const { sent, of } = await response.json();
    await refreshHealth();
    const why = one("[data-health]") ? "The table says why not." : "The Health page says why not.";
    say(button, sent === of ? "Sent. It should be there now." : `Sent to ${sent} of ${of}. ${why}`);
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
    ["[data-trim-chat-later]", trimChatLater],
    ["[data-trim-video], [data-trim-chat-video], [data-trim-subtitles], [data-transcript-cut]", markTaken],
    ["[data-side]", chooseSide],
    ["[data-transcribe]", transcribe],
    ["[data-transcript-go]", playLine],
    ["[data-switch] button", flip],
    ['[data-setting][data-kind="switch"] button', chooseSetting],
    ["[data-chip-add]", addFromButton],
    ["[data-chip-remove]", removeItem],
    ["[data-section]", (tab) => showSection(tab)],
    ["[data-notify-kind] button", chooseKind],
    ["[data-notify-save]", saveNotifications],
    ["[data-notify-remove]", removeSecret],
    ["[data-find-chats]", findChats],
    ["[data-chat-pick]", pickChat],
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
    ["[data-delete-go]", deleteIt],
    ["[data-remove-channel]", askToRemoveChannel],
    ["[data-remove-channel-go]", removeChannel],
    ["[data-remove-channel-cancel]", (button) => button.closest("dialog").close()],
    ["[data-save-again]", saveRecordingAgain],
    ["[data-subscriptions-check]", checkSubscriptions],
    ["[data-notify-test]", testNotifications],
    ["[data-find-moments]", findMoments],
    ["[data-make-clips]", makeClips],
    ["[data-make-chat-file]", makeChatFile],
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

  // In the Trim dialog: pressing an end of the stretch takes hold of it,
  // pressing between them takes hold of the whole stretch, which keeps its
  // length wherever it is dragged, and pressing the strip anywhere else
  // goes there; dragging does any of them along it.
  document.addEventListener("pointerdown", (event) => {
    const strip = event.target.closest("[data-trim-strip]");
    if (!strip || event.button !== 0) return;
    // Else the browser may start selecting text, or drag what was selected,
    // and take the pointer away halfway.
    event.preventDefault();
    const dialog = trimOf(strip);
    const time = trimPoint(strip, event);
    strip.setPointerCapture(event.pointerId);
    trimDrag = trimHeld(strip, event);
    // Without the press's own focus, a field being typed into is left -
    // which sets the end typed into it - and a handle held has the arrows.
    const handle = trimDrag === "from" || trimDrag === "to" ? one(`[data-trim-handle="${trimDrag}"]`, strip) : null;
    if (handle) handle.focus({ preventScroll: true });
    else if (dialog.contains(document.activeElement)) document.activeElement.blur();
    // Whatever is held is held where it was pressed, not by its edge.
    trimPress = null;
    if (trimDrag === "both") {
      trimGrab = time - trimAt(dialog).from;
      trimPress = { x: event.clientX, time };
      strip.classList.add("is-holding");
    } else if (trimDrag) trimGrab = time - trimAt(dialog)[trimDrag];
    else seekTrim(dialog, time);
  });

  document.addEventListener("pointermove", (event) => {
    const strip = event.target.closest("[data-trim-strip]");
    if (!strip) return;
    if (!strip.hasPointerCapture(event.pointerId)) {
      // What a press would take hold of shows as the pointer passes over.
      const held = trimHeld(strip, event);
      strip.dataset.trimOver = held === "both" ? "stretch" : held ? "end" : "";
      return;
    }
    const time = trimPoint(strip, event);
    if (trimPress && Math.abs(event.clientX - trimPress.x) < TRIM_STIR_PIXELS) return;
    trimPress = null;
    if (trimDrag === "both") moveTrimStretch(trimOf(strip), time - trimGrab);
    else if (trimDrag) moveTrim(trimOf(strip), trimDrag, time - trimGrab);
    else seekTrim(trimOf(strip), time);
  });

  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => {
      const held = trimDrag;
      trimDrag = null;
      for (const strip of all("[data-trim-strip].is-holding")) strip.classList.remove("is-holding");
      // The stretch pressed and let go of without moving: the press goes there.
      if (held === "both" && trimPress && type === "pointerup") {
        const dialog = one("[data-trim][open]");
        if (dialog) seekTrim(dialog, trimPress.time);
      }
      trimPress = null;
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
    // The shutdown dialog's keys are the browser's own (Escape closes it).
    if (one("[data-shutdown][open]")) return;
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
    if (target.matches("[data-transcript-line]")) {
      transcriptKey(target, event);
      return;
    }
    // Enter beside a list adds what was typed to it, rather than save the
    // form the field is in.
    if (target.matches("[data-chip-input]") && event.key === "Enter") {
      event.preventDefault();
      addTyped(target);
      return;
    }
    // Enter in where notifications go saves that, rather than the form of
    // detection it stands in.
    if (target.matches("[data-notify-field]") && event.key === "Enter") {
      event.preventDefault();
      saveNotifications(target);
      return;
    }
    if (target.matches("[data-section]") && sectionKey(target, event)) return;
    if (target.matches("[data-strip]") && stripKey(target, event)) return;
    if (target.matches("[data-rec-strip]") && recordingKey(target, event)) return;
    if (event.repeat || target.matches("input, textarea, select") || !one(".review")) return;

    const key = event.key.toLowerCase();
    if (deleteKey(key)) event.preventDefault();
    else if (key === "j") move(1);
    else if (key === "k") move(-1);
    else if (key.length === 1 && key >= "1" && key <= "5") rateFromKeyboard(Number(key));
    else if (key === " " && !target.matches("button, a.ch-btn, a.ch-tab")) {
      event.preventDefault();
      togglePlay(one("[data-clip]"));
    }
  });

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (target.matches("[data-note], [data-transcript-line]")) {
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
    } else if (target.matches("[data-chip-input]")) {
      say(target, "");
    } else if (target.matches("[data-notify-field]")) {
      delete target.dataset.remove;
      say(notifyPane(target), "Not saved yet.");
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
    else if (target.matches("[data-transcript-line]")) correctLine(target);
    else if (target.matches("[data-trim-type]")) typeTrim(target);
    else if (target.matches("[data-trim-chat]")) wantTrimChat(target);
    else if (target.matches("[data-trim-where]")) {
      keepTrimChoice(TRIM_WHERE_KEY, target.value);
      placeTrimChat(trimOf(target));
      paintTrim(trimOf(target));
    }
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
  // The box of chat over the picture follows the picture's shape and size.
  document.addEventListener(
    "loadedmetadata",
    (event) => event.target.matches?.("[data-trim-clip]") && placeTrimChat(trimOf(event.target)),
    true,
  );
  window.addEventListener("resize", () => {
    for (const dialog of all("[data-trim]")) if (dialog.open) placeTrimChat(dialog);
  });
  document.addEventListener("pointerdown", (event) => {
    const overlay = event.target.closest?.("[data-trim-overlay]");
    if (!overlay || event.button !== 0) return;
    event.preventDefault();
    holdTrimBox(overlay, event);
  });
  document.addEventListener("pointermove", (event) => {
    if (trimHold) dragTrimBox(event);
  });
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => {
      if (trimHold) letGoOfTrimBox();
    });
  }
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
    correctLine(document.activeElement);
  });

  dress();
  currentRow()?.scrollIntoView({ block: "nearest" });
  if (one("[data-news]")) setInterval(checkForNew, NEWS_INTERVAL_MS);
  one('.rec-row[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  if (one("[data-recording-clip]")) paintRecording(one("[data-recording-clip]"));
  loadRecordingTrace();
  watchFound();
  watchChatFile();
  if (one('[data-recording-state="running"], [data-recording-state="saving"], [data-recording-row]')) watchRecordings();
  if (one("[data-always-record], [data-recordings]")) {
    setInterval(() => {
      if (recordingsWatch === null && recordingsAwaited()) checkRecordings();
    }, RECORDINGS_AWAITED_POLL_MS);
  }
  if (one("[data-health]")) setInterval(watchHealth, HEALTH_POLL_MS);
})();
