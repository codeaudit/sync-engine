import os.path
import logging as log
import time

import tornado.ioloop
import tornado.web
import tornado.log
import tornado.gen

from securecookie import SecureCookieSerializer
from idler import Idler

import google_oauth

import sessionmanager

from tornadio2 import SocketConnection, TornadioRouter, event
from socket_rpc import SocketRPC
from tornadio2 import proto

import encoding
import api  # This is the handler for RPC calls


COOKIE_SECRET = "32oETzKXQAGaYdkL5gEmGeJJFuYh7EQnp2XdTP1o/Vo="


class Application(tornado.web.Application):
    def __init__(self):

        PATH_TO_ANGULAR = os.path.join(os.path.dirname(__file__), "../angular")
        PATH_TO_STATIC = os.path.join(os.path.dirname(__file__), "../static")

        settings = dict(
            static_path=os.path.join(PATH_TO_STATIC),
            xsrf_cookies=False,  # debug

            debug=True,
            flash_policy_port=843,
            flash_policy_file=os.path.join(PATH_TO_STATIC + "/flashpolicy.xml"),
            socket_io_port=8001,

            login_url="/",  # for now
            redirect_uri="http://localhost:8888/auth/authdone",

            cookie_secret=COOKIE_SECRET,
        )

        PingRouter = TornadioRouter(WireConnection, namespace='wire')

        handlers = PingRouter.apply_routes([
            (r"/", MainHandler),

            (r"/auth/authstart", AuthStartHandler),
            (r"/auth/authdone", AuthDoneHandler),

            (r"/auth/logout", LogoutHandler),

            (r'/app/(.*)', AngularStaticFileHandler, {'path': PATH_TO_ANGULAR,
                                           'default_filename':'index.html'}),
            (r'/app', AppRedirectHandler),
            (r'/file_download', FileDownloadHandler),
            (r'/file_upload', FileUploadHandler),

            (r'/message', MessageHandler),

            (r'/(?!wire|!app|file)(.*)', tornado.web.StaticFileHandler, {'path': PATH_TO_STATIC,
                                           'default_filename':'index.html'}),
        ])

        tornado.web.Application.__init__(self, handlers, **settings)


class BaseHandler(tornado.web.RequestHandler):
    # TODO put authentication stuff here
    def get_current_user(self):
        session_key = self.get_secure_cookie("session")
        return sessionmanager.get_user_from_session(session_key)


class MainHandler(BaseHandler):

    def get(self):
        self.render("templates/index.html", name = self.current_user if self.current_user else " ",
                                            logged_in = bool(self.current_user) )


class AuthStartHandler(BaseHandler):

    @tornado.web.asynchronous
    def get(self):
        url = google_oauth.authorize_redirect_url(
                        self.settings['redirect_uri'])
        self.redirect(url)


class AuthDoneHandler(BaseHandler):
    @tornado.web.asynchronous
    def get(self):
        if not self.get_argument("code", None): self.fail()

        authorization_code = self.get_argument("code", None)

        response = google_oauth.get_authenticated_user(
                            authorization_code,
                            redirect_uri=self.settings['redirect_uri'])

        try:
            assert 'email' in response
            assert 'access_token' in response
            assert 'refresh_token' in response
        except AssertionError, e:
            log.error("Auth failed")
            raise tornado.web.HTTPError(500, "Google auth failed")
            self.finish()
            return

        sessionmanager.store_access_token(response)
        email_address = response['email']

        session_uuid = sessionmanager.store_session(email_address)

        log.info("Successful login. Setting cookie: %s" % session_uuid)
        self.set_secure_cookie("session", session_uuid)

        self.write("<script type='text/javascript'>parent.close();</script>")  # closes window
        self.flush()
        self.finish()


class LogoutHandler(BaseHandler):
    def get(self):
        self.clear_cookie("session")
        sessionmanager.stop_all_crispins()
        self.redirect("/")


class AngularStaticFileHandler(tornado.web.StaticFileHandler):
    def get(self, path, **kwargs):
        if not self.get_secure_cookie("session"):  # check auth
            self.redirect(self.settings['login_url'])
            return
        super(AngularStaticFileHandler, self).get(path, **kwargs)


    # DEBUG: Don't cache anything right now --
    def set_extra_headers(self, path):
        self.set_header("Cache-control", "no-cache")


class AppRedirectHandler(BaseHandler):
    # TODO put authentication stuff here
    def get(self):
        self.redirect('/app/')


class FileDownloadHandler(BaseHandler):

    def get(self):

        args = self.request.arguments

        uid = args['uid'][0]
        section_index = args['section_index'][0]
        content_type = args['content_type'][0]
        data_encoding = args['encoding'][0]
        filename = args['filename'][0]

        self.set_header ('Content-Type', content_type)
        self.set_header ('Content-Disposition', 'attachment; filename=' + filename)

        # Debug
        crispin_client = sessionmanager.get_crispin_from_email('mgrinich@gmail.com')
        data = crispin_client.fetch_msg_body(uid, section_index, folder='Inbox', )

        decoded = encoding.decode_data(data, data_encoding)
        self.write(decoded)





class MessageHandler(BaseHandler):

    def get(self):

        args = self.request.arguments

        uid = args['uid'][0]
        section_index = args['section_index'][0]
        content_type = args['content_type'][0]
        data_encoding = args['encoding'][0]

        data = api.load_message_body_with_uid(uid, section_index, data_encoding, content_type)

        self.set_header ('Content-Type', 'text/html')
        # self.set_header ('Content-Disposition', 'attachment; filename=' + filename)


        self.write(data)



class FileUploadHandler(BaseHandler):

    def post(self):
        if not self.current_user:
            raise tornado.web.HTTPError(403, "access forbidden")

        try:
            uploaded_file = self.request.files['file'][0]  # wacky

            uploads_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "../uploads/")
            if not os.path.exists(uploads_path):
                os.makedirs(uploads_path)

            write_filename = str(time.mktime(time.gmtime())) +'_' + uploaded_file.filename
            write_path = os.path.join(uploads_path, write_filename)

            f = open(write_path, "w")
            f.write(uploaded_file.body)
            f.close()

            log.info("Uploaded file: %s (%s) to %s" % (uploaded_file.filename, uploaded_file.content_type, write_path))

            # TODO
        except Exception, e:
            log.error(e)
            raise tornado.web.HTTPError(500)





# Websocket
class WireConnection(SocketRPC):
    clients = set()


    def __init__(self, session, endpoint=None):
        self.session = session
        self.endpoint = endpoint
        self.is_closed = False
        self.email_address = None


    def on_open(self, request):
        try:
            s = SecureCookieSerializer(COOKIE_SECRET)
            des = s.deserialize('session', request.cookies['session'].value)
            email_address = sessionmanager.get_user_from_session(des)
            if not email_address:
                raise tornado.web.HTTPError(401)
            self.email_address = email_address
        except Exception, e:
            log.warning("Unauthenticated socket connection attempt")
            raise tornado.web.HTTPError(401)
        log.info("Web client connected.")
        self.clients.add(self)


    def on_close(self):
        log.info("Web client disconnected")
        self.clients.remove(self)


    def close(self):
        """Forcibly close client connection"""
        self.session.close(self.endpoint)
        # TODO: Notify about unconfirmed messages?



    # TODO add authentication thing here to check for session token
    @tornado.gen.engine
    def on_message(self, message_body):
        response_text = self.run(api, message_body)

        # Send the message
        msg = proto.message(self.endpoint, response_text)
        self.session.send_message(msg)



def idler_callback():
    log.info("Received idler callback.")
    for connection in WireConnection.clients:
        connection.send_message_notification()



def startserver(port):

    app = Application()
    app.listen(port)


    tornado.log.enable_pretty_logging()
    tornado.autoreload.start()
    tornado.autoreload.add_reload_hook(stopsubmodules)


    global idler
    # idler = Idler('mgrinich@gmail.com', 'ya29.AHES6ZSUdWE6lrGFZOFSXPTuKqu1cnWKwHnzlerRoL52UZA1m88B3oI',
    #               ioloop=loop,
    #               event_callback=idler_callback,
    #               # folder=crispin_client.all_mail_folder_name())
    #               folder="Inbox")
    # idler.connect()
    # idler.idle()

    # Must do this last
    loop = tornado.ioloop.IOLoop.instance()
    log.info('Starting Tornado on port %s' % str(port))

    loop.start()





def stopsubmodules():
    # if idler:
    #     idler.stop()

    sessionmanager.stop_all_crispins()



def stopserver():
    stopsubmodules()
    # Kill IO loop next iteration
    log.info("Stopping Tornado")
    ioloop = tornado.ioloop.IOLoop.instance()
    ioloop.add_callback(ioloop.stop)
