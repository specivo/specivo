export function sprintComplete() {
        return {
            moveToSprint: '',
            getHxVals() {
                return this.moveToSprint
                    ? JSON.stringify({move_incomplete_to_sprint_id: parseInt(this.moveToSprint)})
                    : '{}';
            }
        };
    }
