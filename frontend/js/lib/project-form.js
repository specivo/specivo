/* The modules a new project starts with: issue tracking, plus the wiki and
   time tracking when they are ticked. */
export function projectModules(wiki, time) {
    var modules = ['issue_tracking'];
    if (wiki) modules.push('wiki');
    if (time) modules.push('time_tracking');
    return modules;
}

/* The body of a create-project request from a form holding name, identifier,
   key, description, parentKey, color, moduleWiki and moduleTime. */
export function projectCreateBody(form) {
    return {
        name: form.name,
        identifier: form.identifier,
        key: form.key,
        description: form.description || null,
        parent_key: form.parentKey || null,
        color: form.color || null,
        modules: projectModules(form.moduleWiki, form.moduleTime)
    };
}
