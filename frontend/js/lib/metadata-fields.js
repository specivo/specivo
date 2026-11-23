/* Form fields for the metadata schemas of an issue.
 *
 * Shared by the issue form (metadataFieldRenderer) and the metadata panel on
 * the issue page (issueMetadataPanel): both turn the JSON Schema properties
 * of the schemas that apply into one field per key.
 */

/** How a JSON Schema property is edited: {inputType, typeLabel, options, placeholder, min, max}. */
function fieldInput(prop, tagsPlaceholder) {
    var input = { inputType: 'text', typeLabel: 'string', options: null, placeholder: '', min: null, max: null };
    if (prop.enum) {
        input.inputType = 'enum'; input.typeLabel = 'enum'; input.options = prop.enum;
    } else if (prop.type === 'integer' || prop.type === 'number') {
        input.inputType = 'number'; input.typeLabel = prop.type; input.min = prop.minimum; input.max = prop.maximum;
    } else if (prop.type === 'boolean') {
        input.inputType = 'boolean'; input.typeLabel = 'boolean';
    } else if (prop.type === 'string' && prop.format === 'date') {
        input.inputType = 'date'; input.typeLabel = 'date';
    } else if (prop.type === 'array' && prop.items && prop.items.enum) {
        input.inputType = 'multiselect'; input.typeLabel = 'multi'; input.options = prop.items.enum;
    } else if (prop.type === 'array') {
        input.inputType = 'tags'; input.typeLabel = 'array'; input.placeholder = tagsPlaceholder;
    } else {
        input.placeholder = prop.description || '';
    }
    return input;
}

/** The field's name: its schema's title; without one, the key reads as a name. */
function fieldLabel(key, prop) {
    var title = typeof prop.title === 'string' ? prop.title.trim() : '';
    return title || key.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
}

/** The value an unset field starts with. */
function emptyValue(inputType) {
    if (inputType === 'tags' || inputType === 'multiselect') return [];
    if (inputType === 'boolean') return false;
    return '';
}

/**
 * The fields of *schemas* in order, and the schema names without repeats.
 *
 * Every key *metadata* does not hold yet is set to its field's empty value
 * (the object is changed in place, so the form can bind to it).
 */
export function resolveSchemaFields(schemas, metadata, tagsPlaceholder) {
    var fields = [];
    var schemaNames = [];
    schemas.forEach(function (schema) {
        if (schemaNames.indexOf(schema.name) === -1) schemaNames.push(schema.name);
        var props = (schema.schema_definition && schema.schema_definition.properties) || {};
        Object.keys(props).forEach(function (key) {
            var prop = props[key];
            var input = fieldInput(prop, tagsPlaceholder);
            fields.push({
                key: key,
                label: fieldLabel(key, prop),
                inputType: input.inputType,
                typeLabel: input.typeLabel,
                options: input.options,
                placeholder: input.placeholder,
                min: input.min,
                max: input.max
            });
            if (!(key in metadata)) metadata[key] = emptyValue(input.inputType);
        });
    });
    return { fields: fields, schemaNames: schemaNames };
}
