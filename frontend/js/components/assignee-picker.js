export function assigneePicker() {
        return {
            open: false,
            search: '',

            matchesSearch(login) {
                if (!this.search) return true;
                return login.toLowerCase().indexOf(this.search.toLowerCase()) !== -1;
            }
        };
    }
