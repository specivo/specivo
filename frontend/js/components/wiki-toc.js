export function wikiToc() {
        return {
            headings: [],
            activeId: '',

            init() {
                var content = document.querySelector('.wiki-content');
                if (!content) return;
                var headers = content.querySelectorAll('h2, h3, h4');
                var items = [];
                for (var i = 0; i < headers.length; i++) {
                    var h = headers[i];
                    if (!h.id) {
                        h.id = 'heading-' + i;
                    }
                    items.push({
                        id: h.id,
                        text: h.textContent.trim(),
                        level: parseInt(h.tagName.charAt(1))
                    });
                }
                this.headings = items;

                if (items.length === 0) return;

                var self = this;
                var observer = new IntersectionObserver(function (entries) {
                    for (var j = 0; j < entries.length; j++) {
                        if (entries[j].isIntersecting) {
                            self.activeId = entries[j].target.id;
                        }
                    }
                }, { rootMargin: '-80px 0px -60% 0px', threshold: 0 });

                for (var k = 0; k < headers.length; k++) {
                    observer.observe(headers[k]);
                }
            }
        };
    }
