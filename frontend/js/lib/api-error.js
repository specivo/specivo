/* The message of an API error response body: the first structured error's
   message, a validation error's first detail, or a plain detail string;
   *fallback* when the body says none of these. */
export function apiErrorMessage(body, fallback) {
    return (body.errors && body.errors[0] && body.errors[0].message)
        || (body.detail && body.detail[0] && body.detail[0].msg)
        || body.detail
        || fallback;
}
