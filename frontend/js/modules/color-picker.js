/* Color picker: toggle selected class on radio change */
export function initColorPicker() {
    var radios = document.querySelectorAll('.sp-color-radio');
    if (!radios.length) return;
    radios.forEach(function (r) {
        r.addEventListener('change', function () {
            document.querySelectorAll('.sp-color-swatch').forEach(function (s) { s.classList.remove('selected'); });
            if (r.checked && r.nextElementSibling) r.nextElementSibling.classList.add('selected');
        });
    });
}
