import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import hashlib
import time

HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}
USER_NAME = os.environ["USER_NAME"]
QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "recursive_loc": 0,
    "graph_commits": 0,
    "loc_query": 0,
}


def daily_readme(birthday):
    """Returns the length of time since `birthday`, e.g. 'XX years, XX months, XX days'."""
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return "{} {}, {} {}, {} {}{}".format(
        diff.years, "year" + format_plural(diff.years),
        diff.months, "month" + format_plural(diff.months),
        diff.days, "day" + format_plural(diff.days),
        " 🎂" if (diff.months == 0 and diff.days == 0) else "",
    )


def format_plural(unit):
    return "s" if unit != 1 else ""


def simple_request(func_name, query, variables):
    """Returns a request, or raises an Exception if the response does not succeed."""
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        return request
    raise Exception(func_name, " has failed with a", request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """Uses GitHub's GraphQL v4 API to return total commit count in a date window."""
    query_count("graph_commits")
    query = """
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }"""
    variables = {"start_date": start_date, "end_date": end_date, "login": USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(
        request.json()["data"]["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
    )


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """Uses GitHub's GraphQL v4 API to return total repository or star count."""
    query_count("graph_repos_stars")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == "repos":
        return request.json()["data"]["user"]["repositories"]["totalCount"]
    elif count_type == "stars":
        return stars_counter(request.json()["data"]["user"]["repositories"]["edges"])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits at a time."""
    query_count("recursive_loc")
    query = """
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }"""
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
    request = requests.post(
        "https://api.github.com/graphql", json={"query": query, "variables": variables}, headers=HEADERS
    )
    if request.status_code == 200:
        repo = request.json()["data"]["repository"]
        if repo["defaultBranchRef"] is not None:
            return loc_counter_one_repo(
                owner, repo_name, data, cache_comment,
                repo["defaultBranchRef"]["target"]["history"],
                addition_total, deletion_total, my_commits,
            )
        return 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception("Too many requests in a short amount of time!\nYou've hit the non-documented anti-abuse limit!")
    raise Exception("recursive_loc() has failed with a", request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (GraphQL can only search 100 commits at a
    time); only counts commits authored by OWNER_ID.
    """
    for node in history["edges"]:
        if node["node"]["author"]["user"] == OWNER_ID:
            my_commits += 1
            addition_total += node["node"]["additions"]
            deletion_total += node["node"]["deletions"]

    if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner, repo_name, data, cache_comment,
        addition_total, deletion_total, my_commits,
        history["pageInfo"]["endCursor"],
    )


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Queries every repository the account has access to (per owner_affiliation)
    60 at a time (larger pages time out; smaller pages trip rate limits).
    Returns the total lines of code across all of them.
    """
    if edges is None:
        edges = []
    query_count("loc_query")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos = request.json()["data"]["user"]["repositories"]
    if repos["pageInfo"]["hasNextPage"]:
        edges += repos["edges"]
        return loc_query(owner_affiliation, comment_size, force_cache, repos["pageInfo"]["endCursor"], edges)
    return cache_builder(edges + repos["edges"], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """Recomputes LOC only for repositories whose commit count has changed since last cached."""
    cached = True
    filename = "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    try:
        with open(filename, "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append("This line is a comment block. Write whatever you want here.\n")
        with open(filename, "w") as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]["node"]["nameWithOwner"].encode("utf-8")).hexdigest():
            try:
                if int(commit_count) != edges[index]["node"]["defaultBranchRef"]["target"]["history"]["totalCount"]:
                    owner, repo_name = edges[index]["node"]["nameWithOwner"].split("/")
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = (
                        repo_hash + " "
                        + str(edges[index]["node"]["defaultBranchRef"]["target"]["history"]["totalCount"]) + " "
                        + str(loc[2]) + " " + str(loc[0]) + " " + str(loc[1]) + "\n"
                    )
            except TypeError:  # repo is empty
                data[index] = repo_hash + " 0 0 0 0\n"

    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """Wipes the cache file. Called when the repo count changes or the file is new."""
    with open(filename, "r") as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, "w") as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node["node"]["nameWithOwner"].encode("utf-8")).hexdigest() + " 0 0 0 0\n")


def force_close_file(data, cache_comment):
    """Saves partial cache data before letting an exception propagate."""
    filename = "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print("There was an error while writing to the cache file. The file,", filename, "has had the partial data saved and closed.")


def stars_counter(data):
    total_stars = 0
    for node in data:
        total_stars += node["node"]["stargazers"]["totalCount"]
    return total_stars


def svg_overwrite(filename, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data, age_data=None):
    """Parses an SVG and overwrites the elements holding live stats, by element id."""
    tree = etree.parse(filename)
    root = tree.getroot()
    LINE_TOTAL = 61

    def fmt(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    repo_s, contrib_s = fmt(repo_data), fmt(contrib_data)
    star_s, commit_s = fmt(star_data), fmt(commit_data)
    follow_s = fmt(follower_data)
    loc_net, loc_a, loc_d = fmt(loc_data[2]), fmt(loc_data[0]), fmt(loc_data[1])

    def set_pair(el, text, dots_len):
        find_and_replace(root, el, text)
        dots_len = max(1, dots_len)
        find_and_replace(root, f"{el}_dots", " " + ("." * dots_len) + " ")

    # ". Repos:" + dots + repo + " {Contributed: " + contrib + "} | Stars:" + dots + stars
    repo_dots = 4
    star_dots = (LINE_TOTAL - 37 - len(repo_s) - len(contrib_s) - len(star_s)) - repo_dots
    set_pair("repo_data", repo_s, repo_dots)
    find_and_replace(root, "contrib_data", contrib_s)
    set_pair("star_data", star_s, star_dots)

    # ". Commits:" + dots + commits + " | Followers:" + dots + followers
    commit_dots = 17
    follow_dots = (LINE_TOTAL - 27 - len(commit_s) - len(follow_s)) - commit_dots
    set_pair("commit_data", commit_s, commit_dots)
    set_pair("follower_data", follow_s, follow_dots)

    # ". Lines of Code on GitHub:" + dots + loc + " ( " + add + "++, " + del + "-- )"
    loc_dots = LINE_TOTAL - 40 - len(loc_net) - len(loc_a) - len(loc_d)
    set_pair("loc_data", loc_net, loc_dots)
    find_and_replace(root, "loc_add", loc_a)
    find_and_replace(root, "loc_del", loc_d)

    if age_data is not None:
        # ". Uptime:" + dots + age   -> pad to the same LINE_TOTAL
        set_pair("age_data", str(age_data), LINE_TOTAL - 11 - len(str(age_data)))
    tree.write(filename, encoding="utf-8", xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """Updates an element's text and pads the preceding dot-run so columns stay aligned."""
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: "", 1: " ", 2: ". "}
        dot_string = dot_map[just_len]
    else:
        dot_string = " " + ("." * just_len) + " "
    if dot_string:
        find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text
    else:
        print(f"Warning: no element with id='{element_id}' found in the SVG -- "
              f"is this an old copy of the SVG that's out of sync with today.py?")


def commit_counter(comment_size):
    """Sums total commits from the cache file built by cache_builder."""
    total_commits = 0
    filename = "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    with open(filename, "r") as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """Returns the account ID and creation timestamp of the user."""
    query_count("user_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }"""
    request = simple_request(user_getter.__name__, query, {"login": username})
    return {"id": request.json()["data"]["user"]["id"]}, request.json()["data"]["user"]["createdAt"]


def follower_getter(username):
    query_count("follower_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }"""
    request = simple_request(follower_getter.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["followers"]["totalCount"])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print("{:<23}".format(" " + query_type + ":"), sep="", end="")
    if difference > 1:
        print("{:>12}".format("%.4f" % difference + " s "))
    else:
        print("{:>12}".format("%.4f" % (difference * 1000) + " ms"))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == "__main__":
    print("Calculation times:")

    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter("account data", user_time)

    birthday_env = os.environ.get("BIRTHDAY")
    age_data = None
    if birthday_env:
        age_data, age_time = perf_counter(daily_readme, datetime.datetime.fromisoformat(birthday_env))
        formatter("age calculation", age_time)
    else:
        print("Note: BIRTHDAY secret is not set -- Uptime will not be updated this run.")

    total_loc, loc_time = perf_counter(loc_query, ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], 7)
    formatter("LOC (cached)", loc_time) if total_loc[-1] else formatter("LOC (no cache)", loc_time)

    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, "stars", ["OWNER"])
    repo_data, repo_time = perf_counter(graph_repos_stars, "repos", ["OWNER"])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    for index in range(len(total_loc) - 1):
        total_loc[index] = "{:,}".format(total_loc[index])

    svg_overwrite("dark_mode.svg", commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], age_data)
    svg_overwrite("light_mode.svg", commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], age_data)



    total_time = user_time + loc_time + commit_time + star_time + repo_time + contrib_time + follower_time
    print("Total function time:", "%.4f" % total_time, "s")
    print("Total GitHub GraphQL API calls:", "{:>3}".format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print("{:<28}".format(" " + funct_name + ":"), "{:>6}".format(count))
