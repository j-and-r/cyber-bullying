from flask import Flask, Response, render_template, redirect, session, request, jsonify, send_file
from flask_session import Session
from flask_cors import CORS
import tweepy
from helper import *
import os
import redis
import datetime
import json
import facebook

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app, resources={r'/*': {'origins': 'http://bully-blocker.herokuapp.com'}})

# Setup files
with open("creds.json", "w+") as f:
    f.write(os.environ['CREDS'])
    f.close()

with open("pyrebase.json", "w+") as f:
    f.write(os.environ['PYREBASE'])
    f.close()

cred = credentials.Certificate("creds.json")
firebase = firebase_admin.initialize_app(cred, name="bully-blocker")

# Fetching env vars
consumer_key = os.environ['TWITTER_KEY']
consumer_secret = os.environ['TWITTER_SECRET']
port = int(os.environ.get('PORT', 5000))
redis_password = os.environ.get('REDIS_PASSWORD')
azure_key = os.environ.get('AZURE_KEY')
hive_key = os.environ.get('HIVE_KEY')

# Setting up Redis session:
SESSION_REDIS = redis.StrictRedis(host='redis-10468.c1.us-east1-2.gce.cloud.redislabs.com', port=10468, password=redis_password)
SESSION_TYPE = 'redis'
app.secret_key = "asfa786esdnccs9ehskentmcs"
app.config.from_object(__name__)
Session(app)

# Setting up dictionaries
p_words = set()
n_words = set()

def load_words():
    n_file = open("./static/dicts/negative-words.txt", "r")
    p_file = open("./static/dicts/positive-words.txt", "r")

    for line in p_file:
        if line[0] != ";" and line != "":
            p_words.add(line.rstrip("\n"))
    p_file.close()

    for line in n_file:
        if line[0] != ";" and line != "":
            n_words.add(line.rstrip("\n"))
    n_file.close()

load_words()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kws):
        if not 'user' in session or session['user'] is None:
            return redirect("/sign-in")
        return f(*args, **kws)
    return decorated_function

@app.route("/humans.txt")
def humans():
    return send_file("./static/humans.txt")

# @app.route("/favicon.ico")
# def favicon():
#     return send_file("./static/favicon.ico")

# API routes

@app.route("/moderate", methods=["GET"])
def moderate_tweet():
    text = request.args.get("text")
    # TODO: Replace 0.7 with user threshold or other meaningful value.
    result = moderate(text, azure_key, 0.7)
    return result, 200

# Pages that don't require users to have account:

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/getting-started")
def getting_started():
    logged_in = 'user' in session
    if logged_in:
        logged_in = not session['user'] is None
    return render_template("getting-started.html", logged_in=logged_in)

@app.route("/sign-in", methods=["GET", "POST"])
def sign_in():
    if request.method == "GET":
        return render_template("sign-in.html", err="")
    else:
        email = request.form['email']
        password = request.form['password']
        user = sign_in_user(email, password)
        session['user'] = user
        return redirect('/loading-feed')

@app.route("/sign-up", methods=["GET", "POST"])
def sign_up():
    if request.method == "GET":
        return render_template("sign-up.html", err="")
    else:
        email = request.form['email']
        password = request.form['password']
        confirm = request.form['password-confirm']
        err = new_user(firebase, email, password)
        if err is not "":
            return render_template("sign-up.html", err=err)
        else:
            user = sign_in_user(email, password)
            session['user'] = user
            return redirect("/getting-started")

@app.route("/logout")
def logout():
    if 'user' in session and session['user'] is not None:
        session['user'] = None
    return redirect("/sign-in")



# Front end for logged in users:

@app.route("/twitter-auth")
@login_required
def twitter_auth():
    auth = tweepy.OAuthHandler(consumer_key, consumer_secret)

    try:
        redirect_url = auth.get_authorization_url()
    except tweepy.TweepError:
        return 'Error! Failed to get request token.'

    session['request_token'] = auth.request_token
    return redirect(redirect_url, code=302)

@app.route("/twitter-callback")
@login_required
def twitter_callback():
    if not 'request_token' in session:
        return redirect('/twitter-auth')

    verifier = request.args.get('oauth_verifier')
    auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
    token = session.get('request_token')
    session.pop('request_token')
    auth.request_token = token

    try:
        auth.get_access_token(verifier)
    except tweepy.TweepError:
        return "error"

    session['access_token'] = auth.access_token
    session['access_secret'] = auth.access_token_secret

    return redirect("/loading-feed")

@app.route("/loading-feed")
@login_required
def loading_feed():
    return render_template("loading-feed.html")

@app.route("/twitter-feed")
@login_required
def feed():
    if not 'access_token' in session or not 'access_secret' in session:
        return redirect('/twitter-auth')
    key = session['access_token']
    secret = session['access_secret']

    auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
    auth.set_access_token(key, secret)

    feed = twitter_feed(auth)
    tweets = []
    bodies = []

    for tweet in feed:
        pics = twitter_pictures(tweet)
        date = tweet.created_at.strftime('%A, %b %Y')
        username = tweet.user.name
        profile_pic = tweet.user.profile_image_url
        link = "https://twitter.com/statuses/" + tweet.id_str
        body = tweet.text
        bodies.append(body)

        rating = rate(body, n_words, p_words)

        if len(pics) > 0:
            is_video = "video" in list(pics)[0]
        else:
            is_video = False

        block = False

        if float(rating) > 0:
            overall = "pos"
        else:
            overall = "neg"

        tweets.append({
            "pics": pics,
            "date": date,
            "username": username,
            "profile_pic": profile_pic,
            "body": body,
            "overall": overall,
            "rating": rating,
            "link": link,
            # "moderation": moderation,
            "is_video": is_video,
            "block": block
        })
    batch = []
    batch_size = 3
    for i in range(len(bodies)):
        batch.append(bodies[i])
        if i % batch_size is batch_size - 1:
            batch_size = len(batch)
            print("Batch Size: {0}".format(batch_size))
            # TODO: Replace 0.6 with user threshold.
            result = batch_moderate(batch, azure_key, 0.6)
            if result["multiple"]:
                for j in range(batch_size):
                    index = i-((batch_size-1)-j)
                    tweets[index]["moderation"] = result["result"][j]
                    tweets[index]["moderation"]["percent"] = result["result"][j]["offensive"] * 100
                    offensive = result["result"][j]["offensive"]
                    if offensive < 0.15:
                        color = "#5cb85c"
                    elif offensive < 0.5:
                        color = "#ecc52c"
                    else:
                        color = "#d9534e"
                    # Replace with thresh
                    tweets[index]["block"] = offensive > 0.6
                    tweets[index]["moderation"]["color"] = color
            else:
                for j in range(batch_size):
                    index = i-((batch_size-1)-j)
                    tweets[index]["moderation"] = result["original"]
                    tweets[index]["moderation"]["rating"] = "not offensive in any way."
                    tweets[index]["moderation"]["percent"] = result["original"]["offensive"] * 100
                    offensive = result["original"]["offensive"]
                    if offensive < 0.15:
                        color = "#5cb85c"
                    elif offensive < 0.5:
                        color = "#ecc52c"
                    else:
                        color = "#d9534e"
                    # Replace with thresh
                    tweets[index]["block"] = offensive > 0.6
                    tweets[index]["moderation"]["color"] = color
            batch = []

    if not len(batch) is 0:
        batch_size = len(batch)
        print("Batch Size: {0}".format(batch_size))
        result = batch_moderate(batch, azure_key, 0.6)
        if result["multiple"]:
            for j in range(batch_size):
                index = i-((batch_size-1)-j)
                tweets[index]["moderation"] = result["result"][j]
                tweets[index]["moderation"]["percent"] = result["result"][j]["offensive"] * 100
                offensive = result["result"][j]["offensive"]
                if offensive < 0.33:
                    color = "#5cb85c"
                elif offensive < 0.66:
                    color = "#ecc52c"
                else:
                    color = "#d9534e"
                # Replace with thresh
                tweets[index]["block"] = offensive > 0.6
                tweets[index]["moderation"]["color"] = color
        else:
            for j in range(batch_size):
                index = i-((batch_size-1)-j)
                tweets[index]["moderation"] = result["original"]
                tweets[index]["moderation"]["rating"] = "not offensive in any way."
                tweets[index]["moderation"]["percent"] = result["original"]["offensive"] * 100
                offensive = result["original"]["offensive"]
                if offensive < 0.33:
                    color = "#5cb85c"
                elif offensive < 0.66:
                    color = "#ecc52c"
                else:
                    color = "#d9534e"
                # Replace with thresh
                tweets[index]["block"] = offensive > 0.6
                tweets[index]["moderation"]["color"] = color

    return render_template("twitter-feed.html", tweets=tweets)

@app.route("/twitter-post", methods=["GET", "POST"])
@login_required
def post():
    if not 'access_token' in session or not 'access_secret' in session:
        return redirect('/twitter-auth')

    if request.method == "GET":
        return render_template("create-post.html")
    else:
        body = request.form['body']

        key = session['access_token']
        secret = session['access_secret']
        auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
        auth.set_access_token(key, secret)

        result = post_twitter(auth, body)
        if result is not "":
            return result
        else:
            return redirect("/loading-feed")

@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")

# Tests
@app.route("/generate-password")
def gen_pword():
    return generate_password()

@app.route("/password-strength")
def pwd_strength():
    return render_template("password-strength.html")

@app.route("/feed-test")
def feed_test():
    feed = json.loads(open("feed.txt", "r").read())
    tweets = []
    bodies = []

    for tweet in feed:
        tweets.append({
            "pics": tweet["pics"],
            "date": tweet["date"],
            "username": tweet["username"],
            "profile_pic": tweet["profile_pic"],
            "body": tweet["body"],
            "rating": tweet["rating"],
            "link": tweet["link"],
            "moderation": tweet["moderation"],
            "is_video": tweet["is_video"],
            "block": tweet["block"]
        })

    return render_template("twitter-feed.html", tweets=tweets)


app.run(host="0.0.0.0", port=port, debug=True)

# @app.route("/facebook-auth")
# def facebook_auth():
#     client_id = "465011457266482"
#     redirect_uri = "https://bully-blocker.herokuapp.com/get-access-token"
#     # TODO: Change these to random strings
#     state = "{st=123456789, ds=987654321}"
#
#     return redirect("https://www.facebook.com/v3.0/dialog/oauth?client_id=" + client_id+ "&redirect_uri=" + redirect_uri + "&state='" + state + "'")
#
# @app.route("/get-access-token")
# def get_access_token():
#     code = request.args['code']
#     url = "https://graph.facebook.com/oauth/access_token?client_id=" + app_id + "&redirect_uri=" + redirect_uri + "&client_secret=" + app_secret + "&code=" + code
#
# @app.route("/facebook-callback")
# def facebook_callback():
#     access_token = request.args['access_token']
#     graph = facebook.GraphAPI(access_token=access_token, version="2.7")
#     session['graph'] = graph
#     return str(graph.get_object(id='115046399369073'))
