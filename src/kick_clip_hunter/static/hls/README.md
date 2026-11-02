# hls.js

What plays a stream's VOD on the review page. A VOD is an HLS playlist, which few browsers
play by themselves; this library plays it in the others, through Media Source Extensions. The
page loads it the first time a VOD is played.

`hls.light.min.js` is `dist/hls.light.min.js` of the npm package
[hls.js 1.7.3](https://www.npmjs.com/package/hls.js/v/1.7.3), unmodified
(SHA-256 `0251332c00a216a35d7d6919044d60da82b78beb3bd50ac6c662ae74a2f5474b`). The light build
leaves out subtitles, alternate audio and DRM, none of which a VOD played here has.

Copyright (c) 2017 Dailymotion (https://www.dailymotion.com), licensed under the Apache
License, Version 2.0 - see [`LICENSE`](LICENSE).
