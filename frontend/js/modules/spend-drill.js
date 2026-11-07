/* AI spend reports: keep the reader's place when the drill-down swaps in a new level.

   The report page is one htmx swap target (#sp-spend-report): the drill-down's links
   (path, group switch, rows, person cards, pages) are boosted, so a click fetches the
   next level and swaps it in without a full page load; the URL changes with it, so the
   level can be shared and reloaded. After such a swap the new level's heading
   (data-sp-drill-focus) takes the focus, so keyboard and screen reader users land on
   what they opened, and the page scrolls to it. Without JavaScript the links are plain
   page loads and nothing here runs. */

const TARGET_ID = 'sp-spend-report';

export function initSpendDrill() {
    let pending = false;
    document.addEventListener('htmx:beforeRequest', (event) => {
        const elt = event.detail && event.detail.elt;
        pending = !!(elt && elt.closest && elt.closest(`#${TARGET_ID}`));
    });
    document.addEventListener('htmx:afterSettle', () => {
        if (!pending) return;
        pending = false;
        const heading = document.querySelector(`#${TARGET_ID} [data-sp-drill-focus]`);
        if (heading) heading.focus();
    });
}
