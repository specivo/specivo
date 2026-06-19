export function journalReactions(initial) {
        return {
            reactions: initial.reactions || [],
            showPicker: false,
            issueKey: initial.issueKey || '',
            journalId: initial.journalId || 0,
            emojiMap: {thumbs_up: '👍', thumbs_down: '👎', heart: '❤️', rocket: '🚀', eyes: '👀', tada: '🎉'},

            emojiChar(key) {
                return this.emojiMap[key] || key;
            },

            async toggle(emoji) {
                var res = await spFetch('/api/v1/issues/' + this.issueKey + '/journals/' + this.journalId + '/reactions/' + emoji + '/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok) {
                    var data = await res.json();
                    this.updateLocal(emoji, data.added);
                }
            },

            updateLocal(emoji, added) {
                var found = false;
                for (var i = 0; i < this.reactions.length; i++) {
                    if (this.reactions[i].emoji === emoji) {
                        found = true;
                        if (added) {
                            this.reactions[i].count++;
                            this.reactions[i].reacted_by_me = true;
                        } else {
                            this.reactions[i].count--;
                            this.reactions[i].reacted_by_me = false;
                            if (this.reactions[i].count <= 0) {
                                this.reactions.splice(i, 1);
                            }
                        }
                        break;
                    }
                }
                if (!found && added) {
                    this.reactions.push({emoji: emoji, count: 1, reacted_by_me: true});
                }
            }
        };
    }
