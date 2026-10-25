(function () {
  "use strict";
  var root = document.documentElement;
  try { root.style.visibility = "hidden"; } catch (err) {}
  fetch("/api/app/access/status", { cache: "no-store" })
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (data) {
      if (data && data.enabled && !data.local && !data.authorized) {
        location.replace("/pair");
        return;
      }
    })
    .catch(function () { /* 状态接口失败时不锁死页面；数据接口仍会自行拒绝 */ })
    .finally(function () {
      try { root.style.visibility = ""; } catch (err) {}
    });
})();
