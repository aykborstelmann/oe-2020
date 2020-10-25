import re
import os
import random
import smtplib, ssl
import pathlib
import uuid

from copy import deepcopy
from enum import Enum
from threading import Lock
from flask import Flask, render_template, request, make_response, redirect, url_for, jsonify, json
from flask_httpauth import HTTPBasicAuth
from flask_restful import fields, marshal_with
from flask_restful import Resource, Api
from typing import Dict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.security import generate_password_hash, check_password_hash
import jinja2
import database as db
from database import DBHelper, QuestionState

app = Flask(__name__)
lock = Lock()
api = Api(app)

### --------------- Read Config --------------- ###


if "SUPPORT_EMAIL" in os.environ:
    support_mail_address = os.environ["SUPPORT_EMAIL"]
else:
    support_mail_address = ""

if "DATA_FILE_PATH" in os.environ:
    outfile_path = os.environ["DATA_FILE_PATH"]
else:
    outfile_path = os.path.join(pathlib.Path().absolute(), "oe-landingpage/")
email_list_path = os.path.join(outfile_path, "email_file.txt")
quiz_path = os.path.join(outfile_path, "quiz.json")
group_path = os.path.join(outfile_path, "groups.json")
template_path = os.path.join(outfile_path, "mail_to_participants.txt.jinja2")
communication_link_path = os.path.join(outfile_path, "communication_link_path.txt")

if "REGISTER_TOKEN" in os.environ:
    master_token = os.environ["REGISTER_TOKEN"]
else:
    master_token = "TestToken"

if "URL" in os.environ:
    URL = os.environ["URL"]
else:
    # not the most secure default, but none is
    URL = "localhost"

if "ADMIN_PASSWORD" in os.environ:
    admin_password = os.environ["ADMIN_PASSWORD"]
else:
    # not the most secure default, but none is
    admin_password = "admin"


if "DB_CONNECTION" in os.environ:
    DB_CONNECTION = os.environ["DB_CONNECTION"]
else:
    raise ValueError("No DB connection provided")

### --------------- Authentication --------------- ###


auth = HTTPBasicAuth()
users = {
    "admin": generate_password_hash(admin_password),
}
del admin_password


@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username


@app.errorhandler(404)
def return404(e):
    headers = {"Content-Type": "text/html"}
    return make_response(
        render_template("404.html", support_mail_address=support_mail_address), 200, headers
    )


### --------------- API --------------- ###


def get_list_of_communication_links():
    links = []
    with open(communication_link_path, "r") as f:
        while a := f.readline():
            links.append(a.strip())
    return links


class Groups(Resource):
    @auth.login_required
    def get(self):
        """
        Get all the groups
        """
        with DBHelper(DB_CONNECTION) as session:
            return jsonify(session.query(db.Group))  # TODO test

    @auth.login_required
    def post(self):
        """
        Generate groups, given with a minimum size.
        Any overhang will be evenly divided over the groups, starting from the first
        """
        global groups
        size_min = int(request.form["size_min"])

        # get all the email adresses
        with DBHelper(DB_CONNECTION) as session:
            users = session.query(db.User)

            # generate groups
            amount_to_many = users.count() % size_min
            left_over = users[:amount_to_many]
            generated_groups = []
            users = users[amount_to_many:]
            for i in range(0, len(users), size_min):
                generated_groups.append(users[i : i + size_min])
            for i, mail in enumerate(left_over):
                generated_groups[i % len(generated_groups)].append(mail)

            comm_links = get_list_of_communication_links()
            if len(comm_links) < len(generated_groups):
                return make_response(
                    f"Generated {len(generated_groups)}. Not enough communications links, either more of those or larger group size",
                    400,
                )

            groups = []
            for i, group in enumerate(generated_groups):
                g = db.Group(
                    access_hash=str(uuid.uuid4()),
                    users=group,
                    name=f"Group {i}",
                    com_link=comm_links[i],
                )
                groups.append(g)

            try:
                session.query(db.Group).delete()
                session.add_all(groups)
                session.query(
                    db.Quiz
                ).first().game_state = db.GameState.GROUPS_HAVE_BEEN_ASSIGNED
                session.commit()
            except:
                session.rollback()
                raise

            return f"Generated {len(groups)} Groups, with the size distribution of {[len(x.users) for x in groups]}"
        return "no db", 500


api.add_resource(Groups, "/api/group")


def get_active_question(session):
    return (
        session
        .query(db.Question)
        .filter(db.Question.status == QuestionState.ACTIVE)
        .first()
    )


class Group(Resource):
    def get(self, access_hash):
        """
        Get a representaion of the group
        """
        with DBHelper(DB_CONNECTION) as session:
            if (
                group := session
                .query(db.Group)
                .filter(db.Group.access_hash == access_hash)
                .first()
            ) :
                ret_format = request.args.get("format", "html")
                if ret_format == "html":
                    error = request.args.get("error", None)
                    success = request.args.get("success", None)
                    q = get_active_question(session)
                    return make_response(
                        render_template(
                            "group.html",
                            group=group,
                            question=q,
                            error=error,
                            success=success,
                            get_new_data_url=api.url_for(
                                Group, access_hash=access_hash, format="json"
                            ),
                        ),
                        200,
                    )
                elif ret_format == "json":
                    q = get_active_question(session)
                    if q:
                        question_to_send = {"question": q.text, "answers": [a.text for a in q.answers]}
                    else:
                        question_to_send = None
                    current_answer = None
                    if question_to_send is not None:
                        if q.access_hash in [x.question_id for x in group.answers]:
                            current_answer = [
                                x.text for x in group.answers if x.question_id == q.access_hash
                            ][0]
                    return jsonify(
                        **json.loads(
                            json.htmlsafe_dumps(
                                {
                                    "question": question_to_send,
                                    "group_name": group.name,
                                    "answer": current_answer if q else None,
                                }
                            )
                        )
                    )
        return make_response("Invalid access hash", 403)

    def post(self, access_hash):
        """
        Set the answer for the current question
        """
        with DBHelper(DB_CONNECTION) as session:
            if not (
                group := session
                .query(db.Group)
                .filter(db.Group.access_hash == access_hash)
                .first()
            ):
                return make_response("Invalid access hash", 403)
            if "answer" in request.form:
                answer_text = request.form["answer"]
                question = get_active_question(session)
                if not question:
                    return redirect(
                        api.url_for(
                            Group, access_hash=access_hash, error="No question is being played"
                        )
                    )
                if not (
                    answer := session
                    .query(db.AnswerOptions)
                    .filter(
                        db.AnswerOptions.question == question, db.AnswerOptions.text == answer_text
                    )
                    .first()
                ):
                    return redirect(
                        api.url_for(Group, access_hash=access_hash, error="Invalid answer")
                    )
                prev_ans = [x for x in group.answers if x.question_id == question.access_hash]
                for x in prev_ans:
                    group.answers.remove(x)
                group.answers.append(answer)
                session.commit()
                return redirect(api.url_for(Group, access_hash=access_hash, success="Answer saved"))

            if "group_name" in request.form:
                name = request.form["group_name"]
                group.name = name
                session.commit()
                return redirect(api.url_for(Group, access_hash=access_hash, success="Name saved"))
        return make_response("Missing parameter", 400)


api.add_resource(Group, "/group/<string:access_hash>")


class Question(Resource):
    @auth.login_required(optional=True)
    @marshal_with(
        {"question": fields.String, "answers": fields.List(fields.String), "status": fields.String}
    )
    def get(self, access_hash):
        """
        Get a certain question
        """
        with DBHelper(DB_CONNECTION) as session:
            if access_hash == "current":
                return jsonify(get_active_question(session))
            user = auth.current_user()
            if user:
                question = (
                    session
                    .query(db.Question)
                    .filter(db.Question.access_hash == access_hash)
                    .first()
                )
                return question

    def update_question_status(self, access_hash: str, status: QuestionState) -> bool:
        with DBHelper(DB_CONNECTION) as session:
            if status == QuestionState.ACTIVE and get_active_question(session) != None:
                return False
            question = (
                session
                .query(db.Question)
                .filter(db.Question.access_hash == access_hash)
                .first()
            )
            if not question:
                return False
            question.status = status
            if status == QuestionState.FINISHED:
                session.query(db.Quiz).first().last_finished_question = question

            for group in session.query(db.Group):
                group.points = 0
                for answer in group.answers:
                    if answer.correct:
                        group.points += answer.points
            session.commit()
            return True
        return False

    @auth.login_required
    def post(self, access_hash):
        """
        Change the question status
        """
        status = request.form.get("status")
        status = QuestionState[status]

        if self.update_question_status(access_hash, status):
            return make_response("OK", 200)
        return make_response("Could not change status", 400)
    

api.add_resource(Question, "/api/question/<string:access_hash>")


class Quiz(Resource):
    @auth.login_required
    def get(self):
        """
        Get the entire quiz
        """
        with DBHelper(DB_CONNECTION) as session:
            questions = session.query(db.Question)
            return make_response(
                render_template("quiz.html", questions=questions, QuestionState=QuestionState),
                200,
            )

    def reset_quiz(self):
        with DBHelper(DB_CONNECTION) as session:
            session.query(db.Group).delete()
            quiz = session.query(db.Quiz).first()
            if quiz:
                quiz.game_state = db.GameState.NEW
            session.commit()

    def post(self):
        """
        Resets the quiz
        """
        self.reset_quiz()
        return make_response("OK", 200)
    
    def put(self):
        """
        Load the quiz from disk
        """
        self.reset_quiz()
        with DBHelper(DB_CONNECTION) as session:
            session.query(db.Question).delete()
            with open(quiz_path, "r") as f:
                questions = json.load(f)
            qs = []
            for question in questions["questions"]:
                q = db.Question()
                q.access_hash = str(uuid.uuid4())
                q.status = db.QuestionState.NEW
                q.text = question["text"]
                
                for a in question["answers"]:
                    ans = db.AnswerOptions()
                    ans.access_hash = str(uuid.uuid4())
                    ans.text = a["text"]
                    ans.correct = True if a["correct"].lower() == "true" else False
                    ans.points = a["points"]
                    q.answers.append(ans)

                qs.append(q)

            session.add_all(qs)    
            session.commit()
            return make_response("OK", 200)
        return make_response("no db", 500)



api.add_resource(Quiz, "/quiz")


class User(Resource):
    def get(self, access_hash):
        """
        Get the users page
        """
        with DBHelper(DB_CONNECTION) as session:
            user = (
                session.query(db.User).filter(db.User.access_hash == access_hash).first()
            )
            if not user:
                return redirect(url_for("play"))
            game_state = session.query(db.Quiz).first().game_state
            if game_state == db.GameState.NEW:
                return make_response(
                    render_template("user.html"),
                    200,
                )
            elif game_state == db.GameState.GROUPS_HAVE_BEEN_ASSIGNED:
                user = (
                    session
                    .query(db.User)
                    .filter(db.User.access_hash == access_hash)
                    .first()
                )
                if user:
                    return redirect(api.url_for(Group, access_hash=user.group.access_hash))
                return make_response("user not found, please create a new one at /play", 400)
        raise ValueError("Unhandled Gamestate " + game_state)

    def post(self, access_hash):
        """
        Get the users page
        """
        with DBHelper(DB_CONNECTION) as session:
            game_state = session.query(db.Quiz).first().game_state
            user = db.User(access_hash=str(uuid.uuid4()))
            session.add(user)
            if game_state == db.GameState.GROUPS_HAVE_BEEN_ASSIGNED:
                # add user to existing group
                group = random.choice([x for x in session.query(db.Group)])
                group.users.append(user)
            session.commit()
            return redirect(api.url_for(User, access_hash=user.access_hash))


api.add_resource(User, "/user/<string:access_hash>")


@app.route("/play")
def play():
    with DBHelper(DB_CONNECTION) as session:
        game_state = session.query(db.Quiz).first().game_state
    headers = {"Content-Type": "text/html"}
    return make_response(render_template("play.html"), 200, headers)


### --------------- Views --------------- ###


@app.route("/")
@app.route("/index")
def index():
    headers = {"Content-Type": "text/html"}
    return make_response(render_template("index.html"), 200, headers)


@app.route("/resetEverything", methods=["POST"])
@auth.login_required
def resetEverything():
    with DBHelper(DB_CONNECTION) as session:
        session.query(db.Group).delete()
        session.query(db.User).delete()
        session.query(db.Question).delete()
        session.commit()
    return make_response("OK", 200)


@app.route("/register", methods=["GET", "POST"])
def register():
    headers = {"Content-Type": "text/html"}
    if request.method == "POST":
        email = request.form.get("email")
        token = request.form.get("token")
        value = request.form.get("check_consent")
        regex_mail = r"""(?:[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*|"(?:[\x01-\x08\x0b\x0c\x0e-\x1f\x21\x23-\x5b\x5d-\x7f]|\\[\x01-\x09\x0b\x0c\x0e-\x7f])*")@(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?|\[(?:(?:(2(5[0-5]|[0-4][0-9])|1[0-9][0-9]|[1-9]?[0-9]))\.){3}(?:(2(5[0-5]|[0-4][0-9])|1[0-9][0-9]|[1-9]?[0-9])|[a-z0-9-]*[a-z0-9]:(?:[\x01-\x08\x0b\x0c\x0e-\x1f\x21-\x5a\x53-\x7f]|\\[\x01-\x09\x0b\x0c\x0e-\x7f])+)\])"""
        try:
            if re.match(regex_mail, email) and token == master_token and value == "on":
                with lock:
                    with open(email_list_path, "a") as f:
                        f.write(email + "\n")
                    return make_response(
                        render_template("register.html", saved=True), 200, headers
                    )
            return make_response(render_template("register.html", notsaved=True), 200, headers)
        except Exception as e:
            print(e)
            return make_response(render_template("register.html", notsaved=True), 200, headers)
    return make_response(render_template("register.html"), 200, headers)


@app.route("/admin")
@auth.login_required
def admin():
    headers = {"Content-Type": "text/html"}
    return make_response(render_template("admin.html"), 200, headers)


@app.route("/question")
def overlay_question():
    with DBHelper(DB_CONNECTION) as session:
        q = get_active_question(session)
        if not q:
            q = session.query(db.Quiz).first().last_finished_question
        headers = {"Content-Type": "text/html"}
        return make_response(
            render_template("question.html", question=q, QuestionState=QuestionState), 200, headers
        )


@app.route("/scoreboard")
def scoreboard():
    points = []
    with DBHelper(DB_CONNECTION) as session:
        for x in session.query(db.Group):
            points.append((x.name, x.points))
    headers = {"Content-Type": "text/html"}
    return make_response(
        render_template(
            "scoreboard.html", points=list(sorted(points, key=lambda x: x[1], reverse=True))
        ),
        200,
        headers,
    )


### --------------- Main --------------- ###

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0")
