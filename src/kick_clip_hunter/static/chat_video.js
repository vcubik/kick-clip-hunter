// The page a chat video is made from (chat_video.py).
//
// Nothing here runs on a clock. The page is told what second of the clip it
// is and shows chat as it stood then; whoever is stepping it takes a picture
// and asks for the next frame. A new line eases in - it fades up while the
// column makes room for it - and how far along that is depends only on the
// time asked for, so the same second always looks the same.
(() => {
  "use strict";

  // How long a new line takes to come in, in seconds of the clip.
  const ENTER_SECONDS = 0.25;
  // Lines kept laid out: more than fit, the rest have left the top.
  const KEPT = 40;

  const box = document.querySelector("[data-chat]");
  // In the order they arrived, as the page was written.
  const lines = Array.from(box.querySelectorAll("[data-at]"));
  const arrivals = lines.map((line) => Number(line.dataset.at));
  for (const line of lines) line.hidden = true;

  window.chatVideo = {
    // Resolves once the typeface and every picture are there to be drawn,
    // or after `limit` milliseconds if some never arrive. A hidden line's
    // pictures load too: none of them is lazy.
    ready(limit) {
      const pictures = Array.from(document.images, (image) => image.decode().catch(() => {}));
      const everything = Promise.all([document.fonts.ready, ...pictures]);
      const patience = new Promise((resolve) => setTimeout(resolve, limit));
      return Promise.race([everything, patience]).then(() => undefined);
    },

    // Shows chat as it stood `time` seconds into the clip.
    showAt(time) {
      let arrived = 0;
      while (arrived < lines.length && arrivals[arrived] <= time) arrived += 1;

      let stillToCome = 0;
      lines.forEach((line, index) => {
        const shown = index < arrived && index >= arrived - KEPT;
        line.hidden = !shown;
        if (!shown) return;
        const entered = Math.min(1, (time - arrivals[index]) / ENTER_SECONDS);
        line.style.opacity = String(entered);
        if (entered < 1) stillToCome += line.offsetHeight * (1 - entered);
      });
      box.style.transform = `translateY(${Math.round(stillToCome)}px)`;
    },
  };
})();
