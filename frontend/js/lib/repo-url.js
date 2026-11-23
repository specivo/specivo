/* Read a forge repository address: provider, repository full name and web URL.
 *
 * The browser half of specivo/hooks/repo_url.py, which the server uses to
 * check what the form sends. Both are tested against the same table,
 * tests/fixtures/repo_urls.json: change them together.
 *
 * Known hosts (github.com, gitlab.com, codeberg.org) name the provider and
 * give an https URL cut back to the repository root. Any other host is a
 * self-hosted forge: the provider stays the one chosen and the repository
 * comes from the path (GitLab: every segment before "/-/"; others: the first
 * two). SSH forms (git@host:owner/repo.git, ssh://git@host/owner/repo.git)
 * become https web URLs. Anything else gives null.
 */

export var KNOWN_HOSTS = {'github.com': 'github', 'gitlab.com': 'gitlab', 'codeberg.org': 'forgejo'};

var SEGMENT = /^[A-Za-z0-9_.-]+$/;
var SCP = /^[A-Za-z0-9_.-]+@([A-Za-z0-9.-]+):([^/].*)$/;
var HOST = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/;
// scheme://[userinfo@]host[:port][path][?query][#fragment]
var URL_PARTS = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/(?:[^/?#]*@)?([^/?#:]*)(?::([^/?#]*))?([^?#]*)/;
var SSH_SCHEMES = ['ssh', 'git', 'git+ssh'];

function stripGit(segment) {
    return /\.git$/i.test(segment) ? segment.slice(0, -4) : segment;
}

function repoSegments(segments, provider) {
    var repo;
    if (provider === 'gitlab') {
        var cut = segments.indexOf('-');
        repo = cut === -1 ? segments : segments.slice(0, cut);
    } else {
        repo = segments.slice(0, 2);
    }
    if (repo.length < 2) return null;
    repo = repo.slice(0, -1).concat([stripGit(repo[repo.length - 1])]);
    var ok = repo.every(function (s) { return SEGMENT.test(s) && s !== '.' && s !== '..'; });
    return ok ? repo : null;
}

/**
 * The parts of a URL-form address: {scheme, port, host, path}, or null.
 * An SSH address keeps the https scheme and drops its port, which says
 * nothing about the web address.
 */
function splitUrl(text) {
    var m = URL_PARTS.exec(text);
    if (!m) return null;
    var givenScheme = m[1].toLowerCase();
    var isWeb = givenScheme === 'http' || givenScheme === 'https';
    if (!isWeb && SSH_SCHEMES.indexOf(givenScheme) === -1) return null;
    if (!m[2]) return null;
    var givenPort = m[3] || '';
    if (givenPort !== '' && (!/^\d+$/.test(givenPort) || Number(givenPort) > 65535)) return null;
    if (!isWeb) return { scheme: 'https', port: '', host: m[2], path: m[4] || '' };
    // The web port stays.
    return { scheme: givenScheme, port: Number(givenPort) ? ':' + Number(givenPort) : '', host: m[2], path: m[4] || '' };
}

/** The parts of *text* in the SCP form (git@host:owner/repo.git) or a URL form, or null. */
function splitAddress(text) {
    var scp = text.indexOf('://') === -1 ? SCP.exec(text) : null;
    if (scp) return { scheme: 'https', port: '', host: scp[1], path: scp[2] };
    return splitUrl(text);
}

/** The known provider of *host* (without "www."), or null. */
function knownProvider(bare) {
    return Object.prototype.hasOwnProperty.call(KNOWN_HOSTS, bare) ? KNOWN_HOSTS[bare] : null;
}

/**
 * What *raw* says, or null: {provider, knownHost, host, repoFullName, url}.
 * *provider* is the one chosen in the form, used for hosts that are not known.
 */
export function parseRepoAddress(raw, provider) {
    if (typeof raw !== 'string') return null;
    var text = raw.trim();
    if (!text || text.length > 2048 || /\s/.test(text)) return null;

    var parts = splitAddress(text);
    if (!parts) return null;
    var host = parts.host.toLowerCase().replace(/\.$/, '');
    if (!HOST.test(host)) return null;
    var bare = host.indexOf('www.') === 0 ? host.slice(4) : host;
    var known = knownProvider(bare);
    // A known host is always served over https on the default port.
    var origin = known ? 'https://' + bare : parts.scheme + '://' + host + parts.port;
    var effective = known || provider || null;

    var segments = parts.path.split('/').filter(function (s) { return s !== ''; });
    var repo = repoSegments(segments, effective);
    if (!repo) return null;
    var fullName = repo.join('/');
    return {
        provider: effective,
        knownHost: known !== null,
        host: known ? bare : host,
        repoFullName: fullName,
        url: origin + '/' + fullName
    };
}

/** Whether *raw* is written in one of the SSH forms. */
export function isSshAddress(raw) {
    var text = String(raw || '').trim();
    var at = text.indexOf('://');
    if (at !== -1) return SSH_SCHEMES.indexOf(text.slice(0, at).toLowerCase()) !== -1;
    return SCP.test(text);
}
