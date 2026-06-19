export function kanbanScroll() {
        return {
            canScrollLeft: false,
            canScrollRight: false,

            init() {
                var board = this.$refs.board;
                if (!board) return;
                var self = this;
                var update = function () {
                    self.canScrollLeft = board.scrollLeft > 10;
                    self.canScrollRight = board.scrollLeft < board.scrollWidth - board.clientWidth - 10;
                    var wrap = board.closest('.kanban-wrap');
                    if (wrap) {
                        wrap.classList.toggle('has-overflow-right', self.canScrollRight);
                    }
                };
                board.addEventListener('scroll', update);
                window.addEventListener('resize', update);
                // Initial check after render
                this.$nextTick(function () { update(); });
            },

            scrollLeft() {
                var board = this.$refs.board;
                if (board) board.scrollBy({ left: -280, behavior: 'smooth' });
            },

            scrollRight() {
                var board = this.$refs.board;
                if (board) board.scrollBy({ left: 280, behavior: 'smooth' });
            }
        };
    }
