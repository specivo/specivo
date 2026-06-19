export const notifications = {
        unreadCount: 0,

        async refresh() {
            try {
                var res = await spFetch('/api/v1/notifications/unread-count/');
                if (res.ok) {
                    var data = await res.json();
                    this.unreadCount = data.count;
                }
            } catch (_e) {
                /* Silently ignore — notifications are non-critical */
            }
        }
    };

export const sidebar = {
        collapsed: false,

        toggle() {
            this.collapsed = !this.collapsed;
        }
    };
