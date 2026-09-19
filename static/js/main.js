/* KrishiSetu — client helpers (progressive enhancement only; all core
   features work without JS). */
(function () {
  // auto-dismiss toasts
  document.querySelectorAll(".toast").forEach(function (t) {
    setTimeout(function () {
      t.style.transition = "opacity .5s, transform .5s";
      t.style.opacity = "0";
      t.style.transform = "translateY(-6px)";
      setTimeout(function () { t.remove(); }, 500);
    }, 4200);
  });

  // ---- maps: Leaflet over CARTO tiles when available, else the inline SVG fallback ----
  var cfg = window.FARMLINK || {};
  var leafletOK = false;

  function pin(cls, html, hl) {
    return L.divIcon({ className: "", html: "<div class='fl-pin " + cls + (hl ? " hl" : "") + "'>" + html + "</div>",
                       iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -12] });
  }

  function animateAlong(map, line) {
    if (line.length < 2) return;
    var cum = [0];
    for (var i = 1; i < line.length; i++) {
      cum.push(cum[i - 1] + map.distance(line[i - 1], line[i]));
    }
    var total = cum[cum.length - 1];
    if (!total) return;
    var dot = L.marker(line[0], { icon: L.divIcon({ className: "", html: "<div class='fl-dot'></div>",
                                   iconSize: [14, 14], iconAnchor: [7, 7] }), interactive: false, zIndexOffset: 1000 }).addTo(map);
    var start = null, DUR = 14000, seg = 1;
    function frame(ts) {
      if (!start) start = ts;
      var d = (((ts - start) % DUR) / DUR) * total;
      if (d < cum[seg - 1]) seg = 1;
      while (seg < cum.length - 1 && cum[seg] < d) seg++;
      var f = (d - cum[seg - 1]) / ((cum[seg] - cum[seg - 1]) || 1);
      dot.setLatLng([line[seg - 1][0] + (line[seg][0] - line[seg - 1][0]) * f,
                     line[seg - 1][1] + (line[seg][1] - line[seg - 1][1]) * f]);
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function buildMap(el) {
    var d = JSON.parse(el.getAttribute("data-map"));
    var wrap = el.parentNode;
    wrap.classList.add("leaflet-on");
    // attributionControl: false + a manual, prefix-less control — the default Leaflet
    // wordmark link ("Leaflet | ...") ate a chunk of small maps; CSS also collapses the
    // remaining copyright text to a small "i" chip that expands on hover.
    var map = L.map(el, { scrollWheelZoom: false, attributionControl: false });
    L.control.attribution({ prefix: false }).addTo(map);
    var url = cfg.tileUrl + (cfg.cartoKey ? "?key=" + encodeURIComponent(cfg.cartoKey) : "");
    L.tileLayer(url, { maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>' }).addTo(map);
    var bounds = [[d.depot.lat, d.depot.lng]];

    if (d.pairs && d.pairs.length) {            // unplanned pool: pickup -> delivery pairs
      d.pairs.forEach(function (p) {
        L.polyline(p, { color: "#1d63d8", weight: 3, opacity: 0.9, dashArray: "6 6" }).addTo(map);
        L.marker(p[0], { icon: pin("pickup", "P") }).bindPopup("Pickup (farm)").addTo(map);
        L.marker(p[1], { icon: pin("drop", "D") }).bindPopup("Delivery").addTo(map);
        bounds.push(p[0], p[1]);
      });
    } else {
      if (d.line && d.line.length) {
        // route between each pickup (farm/FPO) and its delivery (consumer/bulk buyer), in blue
        // so it reads clearly against the map tiles regardless of road vs straight-line data.
        L.polyline(d.line, { color: "#1d63d8", weight: 5, opacity: 0.9, dashArray: d.road ? null : "8 8" }).addTo(map);
        d.line.forEach(function (pt) { bounds.push(pt); });
      }
      d.stops.forEach(function (s) {
        var cls = s.status === "done" ? "done" : s.kind;
        var hl = d.highlight && s.order_id === d.highlight;
        L.marker([s.lat, s.lng], { icon: pin(cls, s.seq, hl) })
          .bindPopup("<b>" + (s.kind === "pickup" ? "📦 Pickup" : "📍 Delivery") + " · stop " + s.seq + "</b><br>" +
                     s.name + (s.qty ? "<br>" + s.qty + " kg" : "") + (s.order_id ? "<br>order #" + s.order_id : ""))
          .addTo(map);
        bounds.push([s.lat, s.lng]);
      });
    }
    L.marker([d.depot.lat, d.depot.lng], { icon: pin("depot", "D") }).bindPopup("<b>" + d.depot.name + "</b>").addTo(map);
    map.fitBounds(bounds, { padding: [30, 30] });
    if (d.km != null) {
      var ctl = L.control({ position: "bottomleft" });
      ctl.onAdd = function () { var e = L.DomUtil.create("div", "fl-km"); e.textContent = "route ≈ " + d.km + " km"; return e; };
      ctl.addTo(map);
    }
    if (wrap.classList.contains("anim") && d.line && d.line.length > 1) animateAlong(map, d.line);
    wrap._leafletMap = map;   // so the maximize toggle can call invalidateSize() on resize
  }

  if (window.L) {
    document.querySelectorAll(".lmap").forEach(function (el) {
      try { buildMap(el); leafletOK = true; } catch (e) { console.error("Leaflet map failed, using SVG fallback", e); }
    });
  }

  // ---- maximize / fullscreen toggle on every map (pickup pool, routes, tracking legs) ----
  var scrim = document.createElement("div");
  scrim.className = "map-max-scrim";
  document.body.appendChild(scrim);
  var maxed = null;

  function invalidateSoon(wrap) {
    var map = wrap._leafletMap;
    if (map) { setTimeout(function () { map.invalidateSize(); }, 60); }
  }

  function closeMax() {
    if (!maxed) return;
    maxed.classList.remove("is-maxed");
    scrim.style.display = "none";
    document.body.style.overflow = "";
    invalidateSoon(maxed);
    maxed = null;
  }

  function openMax(wrap) {
    if (maxed) closeMax();
    wrap.classList.add("is-maxed");
    scrim.style.display = "block";
    document.body.style.overflow = "hidden";
    invalidateSoon(wrap);
    maxed = wrap;
  }

  document.querySelectorAll(".map-wrap").forEach(function (wrap) {
    // A page can place its own maximize button in the card header (in the route-map grid,
    // next to the title, rather than floating over the tiles) by giving the map-wrap an id
    // and adding a button with class="map-max-btn" data-target="<that id>". Only fall back
    // to an auto-injected overlay button when no such header button claims this map.
    var toggle = function () { if (wrap.classList.contains("is-maxed")) closeMax(); else openMax(wrap); };
    var headerBtn = wrap.id && document.querySelector('.map-max-btn[data-target="' + wrap.id + '"]');
    if (headerBtn) {
      headerBtn.addEventListener("click", toggle);
      return;
    }
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "map-max-btn map-max-btn-overlay";
    btn.title = "Maximize map";
    btn.setAttribute("aria-label", "Maximize map");
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>';
    btn.addEventListener("click", toggle);
    wrap.appendChild(btn);
  });

  scrim.addEventListener("click", closeMax);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeMax(); });

  // SVG fallback: animated delivery vehicle on the route map (tracking page)
  var mapWrap = leafletOK ? null : document.querySelector(".map-wrap.anim .map-fallback svg");
  if (mapWrap) {
    var line = mapWrap.querySelector("polyline");
    if (line && line.getTotalLength) {
      var NS = "http://www.w3.org/2000/svg";
      var dot = document.createElementNS(NS, "circle");
      dot.setAttribute("r", "7");
      dot.setAttribute("fill", "#1b5e20");
      dot.setAttribute("stroke", "#f9a825");
      dot.setAttribute("stroke-width", "3");
      mapWrap.appendChild(dot);
      var len = line.getTotalLength();
      var start = null, DUR = 14000;
      function frame(ts) {
        if (!start) start = ts;
        var t = ((ts - start) % DUR) / DUR;
        var p = line.getPointAtLength(t * len);
        dot.setAttribute("cx", p.x);
        dot.setAttribute("cy", p.y);
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }
  }

  // ---- forecast chart: hover crosshair + tooltip (reads data-points embedded by charts.py) ----
  document.querySelectorAll(".fchart").forEach(function (wrap) {
    var svg = wrap.querySelector(".fchart-svg");
    var tip = wrap.querySelector(".fchart-tip");
    var cursor = wrap.querySelector(".fchart-cursor");
    if (!svg || !tip || !cursor) return;
    var points;
    try { points = JSON.parse(wrap.getAttribute("data-points")); } catch (e) { return; }
    if (!points || !points.length) return;
    var priceUnit = wrap.getAttribute("data-price-unit") || "";
    var demandUnit = wrap.getAttribute("data-demand-unit") || "";
    var line = cursor.querySelector("line");
    var dotPrice = cursor.querySelector(".fchart-dot-price");
    var dotDemand = cursor.querySelector(".fchart-dot-demand");
    var vb = svg.viewBox.baseVal;

    function nearest(px) {
      var best = points[0], bestD = Infinity;
      for (var i = 0; i < points.length; i++) {
        var d = Math.abs(points[i].x - px);
        if (d < bestD) { bestD = d; best = points[i]; }
      }
      return best;
    }

    function show(evt) {
      var rect = svg.getBoundingClientRect();
      var clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
      var clientY = evt.touches ? evt.touches[0].clientY : evt.clientY;
      var relX = clientX - rect.left;
      var px = (relX / rect.width) * vb.width;
      var p = nearest(px);

      line.setAttribute("x1", p.x); line.setAttribute("x2", p.x);
      dotPrice.setAttribute("cx", p.x); dotPrice.setAttribute("cy", p.yp);
      dotDemand.setAttribute("cx", p.x); dotDemand.setAttribute("cy", p.yd);
      cursor.style.opacity = "1";

      tip.innerHTML = "<b>" + p.label + "</b>" +
        "<span class='tip-price'>Price: ₹" + p.price + "/" + priceUnit.split("/").pop() + "</span><br>" +
        "<span class='tip-demand'>Demand: " + p.demand + demandUnit + "</span>";
      tip.classList.add("show");

      var wrapRect = wrap.getBoundingClientRect();
      var tipLeft = (relX / rect.width) * wrapRect.width;
      var tipTop = ((p.yp < p.yd ? p.yp : p.yd) / vb.height) * wrapRect.height;
      tipLeft = Math.min(Math.max(tipLeft, 6), wrapRect.width - 6);
      tip.style.transform = "translate(" + tipLeft + "px, " + Math.max(tipTop - 14, 4) + "px)" +
        (tipLeft > wrapRect.width - 140 ? " translateX(-100%)" : "");
    }

    function hide() {
      cursor.style.opacity = "0";
      tip.classList.remove("show");
    }

    svg.addEventListener("mousemove", show);
    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("touchmove", function (e) { show(e); e.preventDefault(); }, { passive: false });
    svg.addEventListener("touchend", hide);
  });

  // marketplace search: submit on Enter is native; add tiny debounce for select auto-apply
  var form = document.getElementById("filterForm");
  if (form) {
    form.querySelectorAll("select, input[type=checkbox]").forEach(function (el) {
      el.addEventListener("change", function () { form.submit(); });
    });
  }
})();
