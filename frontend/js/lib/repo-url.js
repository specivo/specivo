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
 * What *raw* says, or null: {provider, knownHost, host, repoFullName, url}.
 * *provider* is the one chosen in the form, used for hosts that are not known.
 */
export function parseRepoAddress(raw, provider) {
    if (typeof raw !== 'string') return null;
    var text = raw.trim();
    if (!text || text.length > 2048 || /\s/.test(text)) return null;

    var scheme = 'https';
    var port = '';
    var host;
    var path;
    var scp = text.indexOf('://') === -1 ? SCP.exec(text) : null;
    if (scp) {
        host = scp[1];
        path = scp[2];
    } else {
        var m = URL_PARTS.exec(text);
        if (!m) return null;
        var givenScheme = m[1].toLowerCase();
        var isWeb = givenScheme === 'http' || givenScheme === 'https';
        if (!isWeb && SSH_SCHEMES.indexOf(givenScheme) === -1) return null;
        if (!m[2]) return null;
        var givenPort = m[3] || '';
        if (givenPort !== '' && (!/^\d+$/.test(givenPort) || Number(givenPort) > 65535)) return null;
        host = m[2];
        path = m[4] || '';
        if (isWeb) {
            scheme = givenScheme;
            // The web port stays; an SSH port says nothing about the web address.
            port = Number(givenPort) ? ':' + Number(givenPort) : '';
        }
    }
    host = host.toLowerCase().replace(/\.$/, '');
    if (!HOST.test(host)) return null;
    var bare = host.indexOf('www.') === 0 ? host.slice(4) : host;
    var known = Object.prototype.hasOwnProperty.call(KNOWN_HOSTS, bare) ? KNOWN_HOSTS[bare] : null;
    if (known) {
        host = bare;
        scheme = 'https';
        port = '';
    }
    var effective = known || provider || null;

    var segments = path.split('/').filter(function (s) { return s !== ''; });
    var repo = repoSegments(segments, effective);
    if (!repo) return null;
    var fullName = repo.join('/');
    return {
        provider: effective,
        knownHost: known !== null,
        host: host,
        repoFullName: fullName,
        url: scheme + '://' + host + port + '/' + fullName
    };
}

/** Whether *raw* is written in one of the SSH forms. */
export function isSshAddress(raw) {
    var text = String(raw || '').trim();
    var at = text.indexOf('://');
    if (at !== -1) return SSH_SCHEMES.indexOf(text.slice(0, at).toLowerCase()) !== -1;
    return SCP.test(text);
}
