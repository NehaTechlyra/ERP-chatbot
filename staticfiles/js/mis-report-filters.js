/*
 * Shared behaviour for MIS report filter forms (form[data-mis-date-filter]):
 *   1. Period select enables/disables the custom start/end date fields.
 *   2. Reset button clears ALL filters (reloads the report without a query string).
 *   3. Every select except "period" becomes a searchable Select2 box that puts
 *      the cursor in the search field when it opens (opt out: data-no-select2).
 *   4. Category -> Subcategory -> Type -> Brand cascading, enabled on forms that
 *      contain selects marked data-cascade="category|subcategory|type|brand" and
 *      a <script id="misCascadeData" type="application/json"> data block.
 *      Changing a parent clears its children.
 */
document.addEventListener('DOMContentLoaded', function () {
  var $ = window.jQuery;
  var hasSelect2 = !!($ && $.fn && $.fn.select2);

  function updateDateFields(form) {
    var period = form.querySelector('[name="period"]');
    var dateFields = form.querySelectorAll('[name="start_date"], [name="end_date"]');
    if (!period) {
      return;
    }

    var isCustom = period.value === 'custom';
    dateFields.forEach(function (field) {
      field.disabled = !isCustom;
      field.setAttribute('aria-disabled', String(!isCustom));
    });
  }

  // ---- Select2 (searchable dropdowns with auto-focus) -------------------
  function initSelect2(form) {
    if (!hasSelect2) {
      return;
    }
    var selects = Array.prototype.filter.call(form.querySelectorAll('select'), function (sel) {
      return sel.name !== 'period' &&
        !sel.hasAttribute('data-no-select2') &&
        !sel.classList.contains('select2-hidden-accessible');
    });
    selects.forEach(function (sel) {
      $(sel).select2({ width: '100%', minimumResultsForSearch: 0 });
    });
    // Select2 4.1 rc doesn't reliably focus its search box with newer jQuery
    $(selects).on('select2:open', function () {
      window.setTimeout(function () {
        var field = document.querySelector('.select2-container--open .select2-search__field');
        if (field) {
          field.focus();
        }
      }, 0);
    });
  }

  // ---- Category / Subcategory / Type / Brand cascade ---------------------
  function initCascade(form) {
    var catSel = form.querySelector('[data-cascade="category"]');
    var subSel = form.querySelector('[data-cascade="subcategory"]');
    var typeSel = form.querySelector('[data-cascade="type"]');
    var brandSel = form.querySelector('[data-cascade="brand"]');
    var dataEl = document.getElementById('misCascadeData');
    if (!catSel || !subSel || !typeSel || !brandSel || !dataEl) {
      return;
    }
    var data = JSON.parse(dataEl.textContent || 'null');
    if (!data) {
      return;
    }

    // Values applied by the server on page load
    var initial = {
      subcategory: subSel.value,
      type: typeSel.value,
      brand: brandSel.value
    };

    // Tell Select2 to redraw after the <option>s or value changed
    function sync(sel) {
      if (hasSelect2 && $(sel).data('select2')) {
        $(sel).trigger('change.select2');
      }
    }

    function fill(sel, rows, labelKey, selected) {
      var first = sel.options[0];
      sel.innerHTML = '';
      sel.appendChild(first);
      var keep = '';
      rows.forEach(function (r) {
        var o = document.createElement('option');
        o.value = r.id;
        o.textContent = r[labelKey] || '';
        if (String(r.id) === String(selected)) {
          o.selected = true;
          keep = String(r.id);
        }
        sel.appendChild(o);
      });
      sel.value = keep;
      sync(sel);
    }

    function subRows() {
      var c = catSel.value;
      return c ? data.subcategories.filter(function (s) { return String(s.category_id) === c; })
               : data.subcategories;
    }
    function typeRows() {
      var sc = subSel.value, c = catSel.value;
      if (sc) {
        return data.types.filter(function (t) { return String(t.subcategory_id) === sc; });
      }
      if (c) {
        var ids = {};
        data.subcategories.forEach(function (s) { if (String(s.category_id) === c) { ids[s.id] = true; } });
        return data.types.filter(function (t) { return ids[t.subcategory_id]; });
      }
      return data.types;
    }
    // Brand has no direct link to category/subcategory/type; narrow it by the
    // combinations found on items.
    function brandRows() {
      var c = catSel.value, sc = subSel.value, t = typeSel.value;
      if (!c && !sc && !t) {
        return data.brands;
      }
      var ok = {};
      data.brand_links.forEach(function (l) {
        if (c && String(l.category_id) !== c) { return; }
        if (sc && String(l.subcategory_id) !== sc) { return; }
        if (t && String(l.item_type_id) !== t) { return; }
        ok[l.brand_id] = true;
      });
      return data.brands.filter(function (b) { return ok[b.id]; });
    }

    function build(sel) {
      fill(subSel, subRows(), 'subcategory_name', sel.subcategory);
      fill(typeSel, typeRows(), 'type_name', sel.type);
      fill(brandSel, brandRows(), 'brand_name', sel.brand);
    }

    // Select2 fires jQuery events (not native ones), so bind through jQuery when present
    function onChange(el, fn) {
      if (hasSelect2) { $(el).on('change', fn); } else { el.addEventListener('change', fn); }
    }

    onChange(catSel, function () {
      build({ subcategory: '', type: '', brand: '' });
    });
    onChange(subSel, function () {
      fill(typeSel, typeRows(), 'type_name', '');
      fill(brandSel, brandRows(), 'brand_name', '');
    });
    onChange(typeSel, function () {
      fill(brandSel, brandRows(), 'brand_name', '');
    });

    build(initial);
  }

  document.querySelectorAll('[data-mis-date-filter]').forEach(function (form) {
    var period = form.querySelector('[name="period"]');
    if (period) {
      period.addEventListener('change', function () {
        updateDateFields(form);
      });
      updateDateFields(form);
    }

    // Reset = clear every filter. The browser's native reset only restores the
    // values the page was loaded with (i.e. the filters already applied), so
    // reload the report without a query string instead.
    form.addEventListener('reset', function (event) {
      event.preventDefault();
      var target = new URL(form.getAttribute('action') || window.location.href, window.location.href);
      target.search = '';
      target.hash = '';
      window.location.assign(target.toString());
    });

    initSelect2(form);
    initCascade(form);
  });
});
