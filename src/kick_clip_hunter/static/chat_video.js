// The page chat is laid out on for a chat video (chat_video.py).
//
// Every line is laid out once, one under another, and stays where it is:
// whoever drives the page takes pictures of the column a screenful at a
// time and puts the frames together from them. Nothing here runs on a clock.
(() => {
  "use strict";

  window.chatVideo = {
    // Resolves once the typeface and every picture are there to be drawn,
    // or after `limit` milliseconds if some never arrive. None of the
    // pictures is lazy: the page is scrolled only to take its pictures.
    ready(limit) {
      const pictures = Array.from(document.images, (image) => image.decode().catch(() => {}));
      const everything = Promise.all([document.fonts.ready, ...pictures]);
      const patience = new Promise((resolve) => setTimeout(resolve, limit));
      return Promise.race([everything, patience]).then(() => undefined);
    },

    // Each line, in the order they arrive: the second it arrives at and
    // where it starts and ends, in pixels from the top - and how many
    // pictures `page` pixels tall the column takes. The page is made that
    // many pictures tall, so that it can be scrolled to the top of each.
    layOut(page) {
      const lines = Array.from(document.querySelectorAll("[data-at]"), (line) => {
        const box = line.getBoundingClientRect();
        return [Number(line.dataset.at), box.top + window.scrollY, box.bottom + window.scrollY];
      });
      const pages = Math.max(1, Math.ceil(document.documentElement.scrollHeight / page));
      document.body.style.minHeight = `${pages * page}px`;
      return { lines, pages };
    },
  };
})();
