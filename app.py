import copy
import json
import os
import logging
import uuid
import httpx
import asyncio
import shutil
import time
import hashlib
from quart import (
    Blueprint,
    Quart,
    jsonify,
    make_response,
    request,
    send_from_directory,
    render_template,
    current_app,
)

from openai import AsyncAzureOpenAI
from azure.identity.aio import (
    DefaultAzureCredential,
    get_bearer_token_provider
)
from backend.auth.auth_utils import get_authenticated_user_details
from backend.security.ms_defender_utils import get_msdefender_user_json
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from backend.utils import (
    format_as_ndjson,
    format_stream_response,
    format_non_streaming_response,
    convert_to_pf_format,
    format_pf_non_streaming_response,
)

bp = Blueprint("routes", __name__, static_folder="static", template_folder="static")

cosmos_db_ready = asyncio.Event()

# Setup enhanced logging for CosmosDB debugging
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("app")


def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    
    logger.info("=== Application Initialization ===")
    logger.info(f"Debug mode: {DEBUG}")
    logger.info(f"Chat history enabled: {app_settings.chat_history is not None}")
    
    if app_settings.chat_history:
        logger.info(f"CosmosDB account: {app_settings.chat_history.account}")
        logger.info(f"CosmosDB database: {app_settings.chat_history.database}")
        logger.info(f"CosmosDB container: {app_settings.chat_history.conversations_container}")
        logger.info(f"Feedback enabled: {app_settings.chat_history.enable_feedback}")
    else:
        logger.warning("Chat history is not configured! No CosmosDB settings found.")
    
    @app.before_serving
    async def init():
        try:
            logger.info("Initializing CosmosDB client...")
            app.cosmos_conversation_client = await init_cosmosdb_client()
            if app.cosmos_conversation_client:
                logger.info("CosmosDB client initialized successfully")
                cosmos_db_ready.set()
                logger.info("cosmos_db_ready event set")
            else:
                logger.warning("CosmosDB client initialization returned None")
            
            # Ensure data directory exists
            import os
            
            data_dir = "data"
            if not os.path.exists(data_dir):
                logger.info(f"Creating data directory: {data_dir}")
                os.makedirs(data_dir)
            
            # Check for PDFs in the data directory
            handbook_path = os.path.join(data_dir, "employee_handbook.pdf")
            if not os.path.exists(handbook_path):
                # Look for the file in the site_pdfs directory
                site_pdfs_dir = "site_pdfs"
                site_handbook_path = os.path.join(site_pdfs_dir, "employee_handbook.pdf")
                
                if os.path.exists(site_handbook_path):
                    logger.info(f"Copying {site_handbook_path} to {handbook_path}")
                    shutil.copy2(site_handbook_path, handbook_path)
                    logger.info(f"Successfully copied employee handbook PDF to {handbook_path}")
                else:
                    logger.warning(f"Employee handbook PDF not found at {site_handbook_path}")
                    # Search for PDFs in common locations
                    pdf_locations = [
                        ".", 
                        "site_pdfs", 
                        "data",
                        "static",
                        "static/pdfs",
                        "pdfs"
                    ]
                    found_pdfs = []
                    for location in pdf_locations:
                        if os.path.exists(location):
                            for root, dirs, files in os.walk(location):
                                for file in files:
                                    if file.lower().endswith(".pdf"):
                                        pdf_path = os.path.join(root, file)
                                        found_pdfs.append(pdf_path)
                                        # Copy the PDF to data directory
                                        target_path = os.path.join(data_dir, file)
                                        if not os.path.exists(target_path):
                                            logger.info(f"Copying {pdf_path} to {target_path}")
                                            shutil.copy2(pdf_path, target_path)
                
                    if found_pdfs:
                        logger.info(f"Found and copied {len(found_pdfs)} PDFs: {found_pdfs}")
                    else:
                        logger.warning("No PDF files found in common locations")
                        # Removed test PDF generation code
                        logger.info("No PDFs found and test PDF generation is disabled.")
            
        except Exception as e:
            logger.error(f"Failed to initialize CosmosDB client: {str(e)}")
            logger.exception("Full stack trace for CosmosDB initialization failure")
            app.cosmos_conversation_client = None
            raise e
    
    @app.after_serving
    async def cleanup():
        logger.info("Application shutdown - cleaning up resources...")
        
        # Close CosmosDB client if it exists
        if hasattr(app, 'cosmos_conversation_client') and app.cosmos_conversation_client:
            try:
                logger.info("Closing CosmosDB client connection...")
                if hasattr(app.cosmos_conversation_client, 'cosmosdb_client'):
                    await app.cosmos_conversation_client.cosmosdb_client.close()
                    logger.info("CosmosDB client closed successfully")
            except Exception as e:
                logger.error(f"Error closing CosmosDB client: {str(e)}")
        
        # Close any remaining aiohttp sessions
        try:
            import aiohttp
            import asyncio
            import gc
            
            # Force collection to find any lingering sessions
            gc.collect()
            
            # Find all client sessions
            for obj in gc.get_objects():
                if isinstance(obj, aiohttp.ClientSession) and not obj.closed:
                    logger.info(f"Closing unclosed aiohttp ClientSession: {obj}")
                    try:
                        await obj.close()
                    except Exception as e:
                        logger.error(f"Error closing aiohttp session: {str(e)}")
                        
            # Let the event loop process all pending tasks
            pending = asyncio.all_tasks()
            logger.info(f"Waiting for {len(pending)} pending tasks to complete...")
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                
            logger.info("All resources cleaned up")
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")
    
    return app


@bp.route("/")
async def index():
    return await render_template(
        "index.html",
        title=app_settings.ui.title,
        favicon=app_settings.ui.favicon
    )


@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")


@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)


@bp.route("/data/<path:path>")
async def serve_data_files(path):
    """Serve files from the data directory, primarily for PDF viewing."""
    import os
    
    logging.info(f"==== SERVE DATA FILES START: {path} ====")
    logging.info(f"Attempting to serve file: {path} from data directory")
    
    # Check if the file exists
    file_path = os.path.join("data", path)
    logging.info(f"Full file path: {file_path}")
    
    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path}")
        # Try to serve from a different directory as a fallback
        fallback_locations = ["site_pdfs", "static/pdfs", "pdfs"]
        logging.info(f"Trying fallback locations: {fallback_locations}")
        
        for location in fallback_locations:
            alt_path = os.path.join(location, path)
            logging.info(f"Checking alternative location: {alt_path}")
            
            if os.path.exists(alt_path):
                logging.info(f"File found in alternative location: {alt_path}")
                # If file found at alternative location, make a copy to data dir
                os.makedirs("data", exist_ok=True)
                import shutil
                try:
                    shutil.copy2(alt_path, file_path)
                    logging.info(f"File copied from {alt_path} to {file_path}")
                    break
                except Exception as e:
                    logging.error(f"Error copying file: {e}")
        
        # If file still doesn't exist after fallback attempts
        if not os.path.exists(file_path):
            logging.error(f"File not found in any location: {path}")
            logging.info(f"==== SERVE DATA FILES END: {path} - NOT FOUND ====")
            return jsonify({"error": f"File not found: {path}"}), 404
    
    # Get file size
    file_size = os.path.getsize(file_path)
    logging.info(f"File size: {file_size} bytes")
    
    try:
        if path.lower().endswith('.pdf'):
            logging.info(f"Serving PDF file: {path}")
            response = await send_from_directory("data", path)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'inline; filename="{path}"'
            response.headers['Content-Length'] = str(file_size)
            # Add cache headers to improve performance
            response.headers['Cache-Control'] = 'public, max-age=86400'
            logging.info(f"PDF response headers: {dict(response.headers)}")
            logging.info(f"==== SERVE DATA FILES END: {path} - SUCCESS ====")
            return response
        else:
            # For non-PDF files
            logging.info(f"Serving non-PDF file: {path}")
            response = await send_from_directory("data", path)
            logging.info(f"Non-PDF response headers: {dict(response.headers)}")
            logging.info(f"==== SERVE DATA FILES END: {path} - SUCCESS ====")
            return response
    except Exception as e:
        logging.exception(f"Error serving file: {path}")
        logging.info(f"==== SERVE DATA FILES END: {path} - ERROR ====")
        return jsonify({"error": f"Error serving file: {str(e)}"}), 500


@bp.route("/site_pdfs/<path:path>")
async def serve_site_pdfs(path):
    """Serve files from the site_pdfs directory, primarily for PDF viewing."""
    import os
    
    logging.info(f"==== SERVE SITE PDFS START: {path} ====")
    logging.info(f"Attempting to serve file: {path} from site_pdfs directory")
    
    # Check if the file exists
    file_path = os.path.join("site_pdfs", path)
    logging.info(f"Full file path: {file_path}")
    
    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path}")
        logging.info(f"==== SERVE SITE PDFS END: {path} - NOT FOUND ====")
        return jsonify({"error": f"File not found: {path}"}), 404
    
    # Get file size
    file_size = os.path.getsize(file_path)
    logging.info(f"File size: {file_size} bytes")
    
    try:
        if path.lower().endswith('.pdf'):
            logging.info(f"Serving PDF file: {path}")
            response = await send_from_directory("site_pdfs", path)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'inline; filename="{path}"'
            response.headers['Content-Length'] = str(file_size)
            # Add cache headers to improve performance
            response.headers['Cache-Control'] = 'public, max-age=86400'
            logging.info(f"PDF response headers: {dict(response.headers)}")
            logging.info(f"==== SERVE SITE PDFS END: {path} - SUCCESS ====")
            return response
        else:
            # For non-PDF files
            logging.info(f"Serving non-PDF file: {path}")
            response = await send_from_directory("site_pdfs", path)
            logging.info(f"Non-PDF response headers: {dict(response.headers)}")
            logging.info(f"==== SERVE SITE PDFS END: {path} - SUCCESS ====")
            return response
    except Exception as e:
        logging.exception(f"Error serving file: {path}")
        logging.info(f"==== SERVE SITE PDFS END: {path} - ERROR ====")
        return jsonify({"error": f"Error serving file: {str(e)}"}), 500


# Debug settings
USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"


# Frontend Settings via Environment Variables
frontend_settings = {
    "auth_enabled": app_settings.base_settings.auth_enabled,
    "feedback_enabled": (
        app_settings.chat_history and
        app_settings.chat_history.enable_feedback
    ),
    "ui": {
        "title": app_settings.ui.title,
        "logo": app_settings.ui.logo,
        "chat_logo": app_settings.ui.chat_logo or app_settings.ui.logo,
        "chat_title": app_settings.ui.chat_title,
        "chat_description": app_settings.ui.chat_description,
        "show_share_button": app_settings.ui.show_share_button,
        "show_chat_history_button": app_settings.ui.show_chat_history_button,
    },
    "sanitize_answer": app_settings.base_settings.sanitize_answer,
    "oyd_enabled": app_settings.base_settings.datasource_type,
}


# Enable Microsoft Defender for Cloud Integration
MS_DEFENDER_ENABLED = os.environ.get("MS_DEFENDER_ENABLED", "true").lower() == "true"


azure_openai_tools = []
azure_openai_available_tools = []

# Initialize Azure OpenAI Client
async def init_openai_client():
    azure_openai_client = None
    
    try:
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is '{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}'"
            )

        # Endpoint
        if (
            not app_settings.azure_openai.endpoint and
            not app_settings.azure_openai.resource
        ):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_RESOURCE is required"
            )

        endpoint = (
            app_settings.azure_openai.endpoint
            if app_settings.azure_openai.endpoint
            else f"https://{app_settings.azure_openai.resource}.openai.azure.com/"
        )

        # Authentication
        aoai_api_key = app_settings.azure_openai.key
        ad_token_provider = None
        if not aoai_api_key:
            logging.debug("No AZURE_OPENAI_KEY found, using Azure Entra ID auth")
            async with DefaultAzureCredential() as credential:
                ad_token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default"
                )

        # Deployment
        deployment = app_settings.azure_openai.model
        if not deployment:
            raise ValueError("AZURE_OPENAI_MODEL is required")

        # Default Headers
        default_headers = {"x-ms-useragent": USER_AGENT}

        # Remote function calls
        if app_settings.azure_openai.function_call_azure_functions_enabled:
            azure_functions_tools_url = f"{app_settings.azure_openai.function_call_azure_functions_tools_base_url}?code={app_settings.azure_openai.function_call_azure_functions_tools_key}"
            async with httpx.AsyncClient() as client:
                response = await client.get(azure_functions_tools_url)
            response_status_code = response.status_code
            if response_status_code == httpx.codes.OK:
                azure_openai_tools.extend(json.loads(response.text))
                for tool in azure_openai_tools:
                    azure_openai_available_tools.append(tool["function"]["name"])
            else:
                logging.error(f"An error occurred while getting OpenAI Function Call tools metadata: {response.status_code}")

        
        azure_openai_client = AsyncAzureOpenAI(
            api_version=app_settings.azure_openai.preview_api_version,
            api_key=aoai_api_key,
            azure_ad_token_provider=ad_token_provider,
            default_headers=default_headers,
            azure_endpoint=endpoint,
        )

        return azure_openai_client
    except Exception as e:
        logging.exception("Exception in Azure OpenAI initialization", e)
        azure_openai_client = None
        raise e

async def openai_remote_azure_function_call(function_name, function_args):
    if app_settings.azure_openai.function_call_azure_functions_enabled is not True:
        return

    azure_functions_tool_url = f"{app_settings.azure_openai.function_call_azure_functions_tool_base_url}?code={app_settings.azure_openai.function_call_azure_functions_tool_key}"
    headers = {'content-type': 'application/json'}
    body = {
        "tool_name": function_name,
        "tool_arguments": json.loads(function_args)
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(azure_functions_tool_url, data=json.dumps(body), headers=headers)
    response.raise_for_status()

    return response.text

async def init_cosmosdb_client():
    cosmos_conversation_client = None
    logger.info("=== CosmosDB Client Initialization ===")
    
    if app_settings.chat_history:
        logger.info(f"CosmosDB settings found: account={app_settings.chat_history.account}, database={app_settings.chat_history.database}, container={app_settings.chat_history.conversations_container}")
        
        try:
            cosmos_endpoint = (
                f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
            )
            logger.info(f"CosmosDB endpoint URL: {cosmos_endpoint}")

            # Check authentication method
            if not app_settings.chat_history.account_key:
                logger.info("No account key found, using managed identity authentication")
                async with DefaultAzureCredential() as cred:
                    credential = cred
                    logger.info("DefaultAzureCredential created for managed identity authentication")
            else:
                logger.info("Using account key authentication")
                credential = app_settings.chat_history.account_key

            logger.info("Creating CosmosConversationClient...")
            
            # Try to access account first to verify basic connectivity
            from azure.cosmos.aio import CosmosClient
            from azure.cosmos import exceptions
            
            try:
                logger.info("Testing basic account connectivity...")
                async with CosmosClient(cosmos_endpoint, credential=credential) as test_client:
                    # Just try to list databases to verify basic connectivity
                    db_count = 0
                    async for _ in test_client.list_databases():
                        db_count += 1
                    logger.info(f"Successfully connected to CosmosDB account, found {db_count} databases")
            except exceptions.CosmosHttpResponseError as e:
                if e.status_code == 401:
                    logger.error("Authentication failed - check your credentials")
                    raise ValueError("Authentication failed with status code 401") from e
                elif e.status_code == 403:
                    logger.error("Permission denied - your identity doesn't have access rights")
                    raise ValueError("Permission denied with status code 403") from e
                else:
                    logger.error(f"HTTP error {e.status_code} when connecting to CosmosDB: {str(e)}")
                    raise ValueError(f"CosmosDB HTTP error: {str(e)}") from e
            except Exception as e:
                logger.error(f"Error testing CosmosDB connectivity: {str(e)}")
                raise ValueError(f"CosmosDB connection error: {str(e)}") from e
            
            # Now create the client
            logger.info(f"Database and container verified, creating client...")
            logger.info(f"Database name: {app_settings.chat_history.database}")
            logger.info(f"Container name: {app_settings.chat_history.conversations_container}")
            logger.info(f"Enable feedback: {app_settings.chat_history.enable_feedback}")
            
            cosmos_conversation_client = CosmosConversationClient(
                cosmosdb_endpoint=cosmos_endpoint,
                credential=credential,
                database_name=app_settings.chat_history.database,
                container_name=app_settings.chat_history.conversations_container,
                enable_message_feedback=app_settings.chat_history.enable_feedback,
            )
            
            # Test the connection to verify it's working
            logger.info("Testing CosmosDB connection...")
            try:
                success, error = await cosmos_conversation_client.ensure()
                if success:
                    logger.info("CosmosDB connection test successful!")
                else:
                    logger.error(f"CosmosDB connection test failed: {error}")
                    # If the database or container don't exist, we should create them
                    if "database" in error.lower() and "not found" in error.lower():
                        logger.warning("Database not found - may need to be created")
                    elif "container" in error.lower() and "not found" in error.lower():
                        logger.warning("Container not found - may need to be created")
                    await cosmos_conversation_client.close()
                    cosmos_conversation_client = None
            except Exception as e:
                logger.error(f"Error testing CosmosDB connection: {str(e)}")
                logger.exception("CosmosDB connection test exception")
                if cosmos_conversation_client:
                    await cosmos_conversation_client.close()
                cosmos_conversation_client = None
                
        except Exception as e:
            logger.error(f"Exception in CosmosDB initialization: {str(e)}")
            logger.exception("Full stack trace for CosmosDB initialization exception")
            if cosmos_conversation_client:
                await cosmos_conversation_client.close()
            cosmos_conversation_client = None
            raise e
    else:
        logger.warning("CosmosDB not configured - no chat_history settings found")

    return cosmos_conversation_client


def prepare_model_args(request_body, request_headers):
    request_messages = request_body.get("messages", [])
    messages = []
    if not app_settings.datasource:
        messages = [
            {
                "role": "system",
                "content": app_settings.azure_openai.system_message
            }
        ]

    for message in request_messages:
        if message:
            match message["role"]:
                case "user":
                    messages.append(
                        {
                            "role": message["role"],
                            "content": message["content"]
                        }
                    )
                case "assistant" | "function" | "tool":
                    messages_helper = {}
                    messages_helper["role"] = message["role"]
                    if "name" in message:
                        messages_helper["name"] = message["name"]
                    if "function_call" in message:
                        messages_helper["function_call"] = message["function_call"]
                    messages_helper["content"] = message["content"]
                    if "context" in message:
                        context_obj = json.loads(message["context"])
                        messages_helper["context"] = context_obj
                    
                    messages.append(messages_helper)


    user_json = None
    if (MS_DEFENDER_ENABLED):
        authenticated_user_details = get_authenticated_user_details(request_headers)
        conversation_id = request_body.get("conversation_id", None)
        application_name = app_settings.ui.title
        user_json = get_msdefender_user_json(authenticated_user_details, request_headers, conversation_id, application_name)

    model_args = {
        "messages": messages,
        "temperature": app_settings.azure_openai.temperature,
        "max_tokens": app_settings.azure_openai.max_tokens,
        "top_p": app_settings.azure_openai.top_p,
        "stop": app_settings.azure_openai.stop_sequence,
        "stream": app_settings.azure_openai.stream,
        "model": app_settings.azure_openai.model,
        "user": user_json
    }

    if len(messages) > 0:
        if messages[-1]["role"] == "user":
            if app_settings.azure_openai.function_call_azure_functions_enabled and len(azure_openai_tools) > 0:
                model_args["tools"] = azure_openai_tools

            if app_settings.datasource:
                model_args["extra_body"] = {
                    "data_sources": [
                        app_settings.datasource.construct_payload_configuration(
                            request=request
                        )
                    ]
                }

    model_args_clean = copy.deepcopy(model_args)
    if model_args_clean.get("extra_body"):
        secret_params = [
            "key",
            "connection_string",
            "embedding_key",
            "encoded_api_key",
            "api_key",
        ]
        for secret_param in secret_params:
            if model_args_clean["extra_body"]["data_sources"][0]["parameters"].get(
                secret_param
            ):
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    secret_param
                ] = "*****"
        authentication = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("authentication", {})
        for field in authentication:
            if field in secret_params:
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    "authentication"
                ][field] = "*****"
        embeddingDependency = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("embedding_dependency", {})
        if "authentication" in embeddingDependency:
            for field in embeddingDependency["authentication"]:
                if field in secret_params:
                    model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                        "embedding_dependency"
                    ]["authentication"][field] = "*****"

    logging.debug(f"REQUEST BODY: {json.dumps(model_args_clean, indent=4)}")

    return model_args


async def promptflow_request(request):
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_settings.promptflow.api_key}",
        }
        # Adding timeout for scenarios where response takes longer to come back
        logging.debug(f"Setting timeout to {app_settings.promptflow.response_timeout}")
        async with httpx.AsyncClient(
            timeout=float(app_settings.promptflow.response_timeout)
        ) as client:
            pf_formatted_obj = convert_to_pf_format(
                request,
                app_settings.promptflow.request_field_name,
                app_settings.promptflow.response_field_name
            )
            # NOTE: This only support question and chat_history parameters
            # If you need to add more parameters, you need to modify the request body
            response = await client.post(
                app_settings.promptflow.endpoint,
                json={
                    app_settings.promptflow.request_field_name: pf_formatted_obj[-1]["inputs"][app_settings.promptflow.request_field_name],
                    "chat_history": pf_formatted_obj[:-1],
                },
                headers=headers,
            )
        resp = response.json()
        resp["id"] = request["messages"][-1]["id"]
        return resp
    except Exception as e:
        logging.error(f"An error occurred while making promptflow_request: {e}")


async def process_function_call(response):
    response_message = response.choices[0].message
    messages = []

    if response_message.tool_calls:
        for tool_call in response_message.tool_calls:
            # Check if function exists
            if tool_call.function.name not in azure_openai_available_tools:
                continue
            
            function_response = await openai_remote_azure_function_call(tool_call.function.name, tool_call.function.arguments)

            # adding assistant response to messages
            messages.append(
                {
                    "role": response_message.role,
                    "function_call": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                    "content": None,
                }
            )
            
            # adding function response to messages
            messages.append(
                {
                    "role": "function",
                    "name": tool_call.function.name,
                    "content": function_response,
                }
            )  # extend conversation with function response
        
        return messages
    
    return None

async def send_chat_request(request_body, request_headers):
    filtered_messages = []
    messages = request_body.get("messages", [])
    for message in messages:
        if message.get("role") != 'tool':
            filtered_messages.append(message)
            
    request_body['messages'] = filtered_messages
    model_args = prepare_model_args(request_body, request_headers)

    try:
        azure_openai_client = await init_openai_client()
        raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
        response = raw_response.parse()
        apim_request_id = raw_response.headers.get("apim-request-id") 
    except Exception as e:
        logging.exception("Exception in send_chat_request")
        raise e

    return response, apim_request_id


async def complete_chat_request(request_body, request_headers):
    if app_settings.base_settings.use_promptflow:
        response = await promptflow_request(request_body)
        history_metadata = request_body.get("history_metadata", {})
        return format_pf_non_streaming_response(
            response,
            history_metadata,
            app_settings.promptflow.response_field_name,
            app_settings.promptflow.citations_field_name
        )
    else:
        response, apim_request_id = await send_chat_request(request_body, request_headers)
        history_metadata = request_body.get("history_metadata", {})
        non_streaming_response = format_non_streaming_response(response, history_metadata, apim_request_id)

        if app_settings.azure_openai.function_call_azure_functions_enabled:
            function_response = await process_function_call(response)  # Add await here

            if function_response:
                request_body["messages"].extend(function_response)

                response, apim_request_id = await send_chat_request(request_body, request_headers)
                history_metadata = request_body.get("history_metadata", {})
                non_streaming_response = format_non_streaming_response(response, history_metadata, apim_request_id)

    return non_streaming_response

class AzureOpenaiFunctionCallStreamState():
    def __init__(self):
        self.tool_calls = []                # All tool calls detected in the stream
        self.tool_name = ""                 # Tool name being streamed
        self.tool_arguments_stream = ""     # Tool arguments being streamed
        self.current_tool_call = None       # JSON with the tool name and arguments currently being streamed
        self.function_messages = []         # All function messages to be appended to the chat history
        self.streaming_state = "INITIAL"    # Streaming state (INITIAL, STREAMING, COMPLETED)


async def process_function_call_stream(completionChunk, function_call_stream_state, request_body, request_headers, history_metadata, apim_request_id):
    if hasattr(completionChunk, "choices") and len(completionChunk.choices) > 0:
        response_message = completionChunk.choices[0].delta
        
        # Function calling stream processing
        if response_message.tool_calls and function_call_stream_state.streaming_state in ["INITIAL", "STREAMING"]:
            function_call_stream_state.streaming_state = "STREAMING"
            for tool_call_chunk in response_message.tool_calls:
                # New tool call
                if tool_call_chunk.id:
                    if function_call_stream_state.current_tool_call:
                        function_call_stream_state.tool_arguments_stream += tool_call_chunk.function.arguments if tool_call_chunk.function.arguments else ""
                        function_call_stream_state.current_tool_call["tool_arguments"] = function_call_stream_state.tool_arguments_stream
                        function_call_stream_state.tool_arguments_stream = ""
                        function_call_stream_state.tool_name = ""
                        function_call_stream_state.tool_calls.append(function_call_stream_state.current_tool_call)

                    function_call_stream_state.current_tool_call = {
                        "tool_id": tool_call_chunk.id,
                        "tool_name": tool_call_chunk.function.name if function_call_stream_state.tool_name == "" else function_call_stream_state.tool_name
                    }
                else:
                    function_call_stream_state.tool_arguments_stream += tool_call_chunk.function.arguments if tool_call_chunk.function.arguments else ""
                
        # Function call - Streaming completed
        elif response_message.tool_calls is None and function_call_stream_state.streaming_state == "STREAMING":
            function_call_stream_state.current_tool_call["tool_arguments"] = function_call_stream_state.tool_arguments_stream
            function_call_stream_state.tool_calls.append(function_call_stream_state.current_tool_call)
            
            for tool_call in function_call_stream_state.tool_calls:
                tool_response = await openai_remote_azure_function_call(tool_call["tool_name"], tool_call["tool_arguments"])

                function_call_stream_state.function_messages.append({
                    "role": "assistant",
                    "function_call": {
                        "name" : tool_call["tool_name"],
                        "arguments": tool_call["tool_arguments"]
                    },
                    "content": None
                })
                function_call_stream_state.function_messages.append({
                    "tool_call_id": tool_call["tool_id"],
                    "role": "function",
                    "name": tool_call["tool_name"],
                    "content": tool_response,
                })
            
            function_call_stream_state.streaming_state = "COMPLETED"
            return function_call_stream_state.streaming_state
        
        else:
            return function_call_stream_state.streaming_state


async def stream_chat_request(request_body, request_headers):
    response, apim_request_id = await send_chat_request(request_body, request_headers)
    history_metadata = request_body.get("history_metadata", {})
    
    async def generate(apim_request_id, history_metadata):
        if app_settings.azure_openai.function_call_azure_functions_enabled:
            # Maintain state during function call streaming
            function_call_stream_state = AzureOpenaiFunctionCallStreamState()
            
            async for completionChunk in response:
                stream_state = await process_function_call_stream(completionChunk, function_call_stream_state, request_body, request_headers, history_metadata, apim_request_id)
                
                # No function call, asistant response
                if stream_state == "INITIAL":
                    yield format_stream_response(completionChunk, history_metadata, apim_request_id)

                # Function call stream completed, functions were executed.
                # Append function calls and results to history and send to OpenAI, to stream the final answer.
                if stream_state == "COMPLETED":
                    request_body["messages"].extend(function_call_stream_state.function_messages)
                    function_response, apim_request_id = await send_chat_request(request_body, request_headers)
                    async for functionCompletionChunk in function_response:
                        yield format_stream_response(functionCompletionChunk, history_metadata, apim_request_id)
                
        else:
            async for completionChunk in response:
                yield format_stream_response(completionChunk, history_metadata, apim_request_id)

    return generate(apim_request_id=apim_request_id, history_metadata=history_metadata)


async def conversation_internal(request_body, request_headers):
    try:
        if app_settings.azure_openai.stream and not app_settings.base_settings.use_promptflow:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500


@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    return await conversation_internal(request_json, request.headers)


@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        logger.info("=== Frontend Settings Request ===")
        logger.info(f"Feedback enabled: {frontend_settings.get('feedback_enabled', False)}")
        logger.info(f"Chat history button shown: {frontend_settings.get('ui', {}).get('show_chat_history_button', False)}")
        
        # Test that the settings are properly serializable
        try:
            serialized = json.dumps(frontend_settings)
            # Try to verify it can be parsed back without issues
            json.loads(serialized)
            logger.info("Frontend settings successfully serialized to JSON")
        except Exception as json_error:
            logger.error(f"Error serializing frontend settings to JSON: {str(json_error)}")
            # Try to identify the problematic field
            for key, value in frontend_settings.items():
                try:
                    json.dumps({key: value})
                except Exception as e:
                    logger.error(f"Field '{key}' has invalid value: {repr(value)}")
            
            # Return a sanitized version with the problematic fields removed
            sanitized_settings = copy.deepcopy(frontend_settings)
            if 'ui' in sanitized_settings:
                for key in ['title', 'chat_title', 'chat_description']:
                    if key in sanitized_settings['ui']:
                        sanitized_settings['ui'][key] = str(sanitized_settings['ui'][key])
            
            logger.info("Returning sanitized frontend settings")
            return jsonify(sanitized_settings), 200
        
        # Dump all frontend settings for debugging
        if DEBUG:
            logger.debug(f"Complete frontend settings: {json.dumps(frontend_settings, indent=2)}")
        
        return jsonify(frontend_settings), 200
    except Exception as e:
        logger.exception("Exception in /frontend_settings")
        return jsonify({"error": str(e)}), 500


## Conversation History API ##
@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await current_app.cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await current_app.cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
            if createdMessageValue == "Conversation not found":
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            raise Exception("No user message found")

        # Submit request to Chat Completions for response
        request_body = await request.get_json()
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        if not conversation_id:
            raise Exception("No conversation_id found")

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool":
                # write the tool message first
                await current_app.cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[-2],
                )
            # write the assistant message
            await current_app.cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            raise Exception("No bot messages found")

        # Submit request to Chat Completions for response
        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for message_id
    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            return jsonify({"error": "message_feedback is required"}), 400

        logger.info(f"Processing feedback for message_id={message_id}, user_id={user_id}, feedback={message_feedback}")

        ## update the message in cosmos
        try:
            # First get the conversation and surrounding messages to log context
            # Find which conversation this message belongs to
            conversation_query = f"SELECT c.conversationId FROM c WHERE c.id = '{message_id}' AND c.type = 'message'"
            conversation_id = None
            
            try:
                # Try direct item read first with the message ID as partition key
                message = await current_app.cosmos_conversation_client.container_client.read_item(
                    item=message_id, 
                    partition_key=message_id
                )
                conversation_id = message.get("conversationId")
                logger.info(f"Found conversation ID {conversation_id} from direct item read")
            except Exception as e:
                logger.info(f"Direct read failed, using query: {str(e)}")
                # Fall back to query if direct read fails
                async for item in current_app.cosmos_conversation_client.container_client.query_items(
                    query=conversation_query
                ):
                    conversation_id = item.get("conversationId")
                    break
                
            if conversation_id:
                # Get related messages
                messages = await current_app.cosmos_conversation_client.get_messages(user_id, conversation_id)
                
                # Find the rated message and previous user message
                rated_message = None
                user_query = None
                
                for i, msg in enumerate(messages):
                    if msg["id"] == message_id:
                        rated_message = msg
                        # Look for the most recent user message before this one
                        for j in range(i-1, -1, -1):
                            if messages[j]["role"] == "user":
                                user_query = messages[j]
                                break
                        break
                
                # Log the feedback with context
                if rated_message and user_query:
                    logger.info("=== FEEDBACK DETAILS ===")
                    logger.info(f"Conversation ID: {conversation_id}")
                    logger.info(f"Feedback: {message_feedback}")
                    logger.info(f"User query: {user_query.get('content', '')[:100]}...")
                    logger.info(f"Assistant answer: {rated_message.get('content', '')[:100]}...")
                else:
                    logger.info(f"Feedback {message_feedback} given for message {message_id}, but couldn't find related messages")
            
            # Update the message with feedback
            updated_message = await current_app.cosmos_conversation_client.update_message_feedback(
                user_id, message_id, message_feedback
            )
            if updated_message:
                logger.info(f"Successfully updated message {message_id} with feedback {message_feedback}")
                return (
                    jsonify(
                        {
                            "message": f"Successfully updated message with feedback {message_feedback}",
                            "message_id": message_id,
                        }
                    ),
                    200,
                )
            else:
                logger.error(f"Unable to update message {message_id} - no update response from CosmosDB")
                return (
                    jsonify(
                        {
                            "error": f"Unable to update message {message_id}. It either does not exist or the user does not have access to it."
                        }
                    ),
                    404,
                )
        except Exception as cosmos_error:
            error_message = str(cosmos_error)
            logger.error(f"CosmosDB error updating message feedback: {error_message}")
            
            if "NotFound" in error_message or "not exist" in error_message:
                # Try to verify if the message exists but with a different partition key
                try:
                    logger.info(f"Checking if message exists with different partition key")
                    from azure.cosmos import exceptions
                    
                    try:
                        # Try a direct query to find the message by ID only
                        query = f"SELECT * FROM c WHERE c.id = '{message_id}'"
                        messages = []
                        # Simplified query without options parameters
                        async for item in current_app.cosmos_conversation_client.container_client.query_items(
                            query=query
                        ):
                            messages.append(item)
                        
                        if messages:
                            found_user_id = messages[0].get("userId", "unknown")
                            logger.info(f"Message found but with different user ID: {found_user_id}")
                            return jsonify({
                                "error": f"Message found but belongs to different user (found: {found_user_id}, requesting: {user_id})"
                            }), 403
                        else:
                            logger.info(f"Message {message_id} not found in any partition")
                            return jsonify({
                                "error": f"Message {message_id} does not exist in the database"
                            }), 404
                            
                    except Exception as query_error:
                        logger.error(f"Error querying for message: {str(query_error)}")
                        
                except Exception as check_error:
                    logger.error(f"Error during message existence check: {str(check_error)}")
                
                return jsonify({
                    "error": f"Message {message_id} not found. It may have been deleted or never existed."
                }), 404
            else:
                # General database error
                return jsonify({
                    "error": f"Database error: {error_message}"
                }), 500

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    await cosmos_db_ready.wait()
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos first
        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        ## Now delete the conversation
        deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
            user_id, conversation_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    await cosmos_db_ready.wait()
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversations from cosmos
    conversations = await current_app.cosmos_conversation_client.get_conversations(
        user_id, offset=offset, limit=25
    )
    if not isinstance(conversations, list):
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    ## return the conversation ids

    return jsonify(conversations), 200


def sanitize_json_content(content):
    """
    Sanitize content to ensure it can be safely serialized to JSON.
    This handles non-UTF8 characters, control characters, and other issues.
    """
    if content is None:
        return ""
        
    # Handle non-string content
    if not isinstance(content, str):
        try:
            return str(content)
        except:
            return "[Content cannot be displayed]"
    
    # Replace problematic characters that might cause JSON issues
    # Replace control characters and non-UTF8 sequences
    sanitized = ""
    for char in content:
        if ord(char) < 32 and char not in '\r\n\t':
            continue  # Skip control characters except newlines and tabs
        sanitized += char
        
    return sanitized

@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)
    
    logger.info(f"Reading history for conversation_id={conversation_id}, user_id={user_id}")

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        logger.error("CosmosDB client is not initialized")
        return jsonify({"error": "CosmosDB is not configured or not working"}), 500

    try:
        ## get the conversation object and the related messages from cosmos
        logger.info(f"Getting conversation with ID: {conversation_id}")
        conversation = await current_app.cosmos_conversation_client.get_conversation(
            user_id, conversation_id
        )
        ## return the conversation id and the messages in the bot frontend format
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for user {user_id}")
            return (
                jsonify(
                    {
                        "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                    }
                ),
                404,
            )

        # get the messages for the conversation from cosmos
        logger.info(f"Getting messages for conversation ID: {conversation_id}")
        conversation_messages = await current_app.cosmos_conversation_client.get_messages(
            user_id, conversation_id
        )
        
        if not conversation_messages:
            logger.info(f"No messages found for conversation ID: {conversation_id}")
            return jsonify({"conversation_id": conversation_id, "messages": []}), 200

        logger.info(f"Found {len(conversation_messages)} messages")
        
        # Log sample message to help diagnose issues
        if len(conversation_messages) > 0:
            sample_msg = conversation_messages[0]
            sanitized_sample = {k: v for k, v in sample_msg.items() if k != 'content'}
            logger.info(f"Sample message structure: {sanitized_sample}")
            logger.info(f"Message keys: {list(sample_msg.keys())}")

        ## format the messages in the bot frontend format
        messages = []
        for msg in conversation_messages:
            try:
                # Sanitize content to ensure JSON safety
                sanitized_content = sanitize_json_content(msg.get("content"))
                
                formatted_msg = {
                    "id": msg["id"],
                    "role": msg["role"],
                    "content": sanitized_content,
                    "createdAt": msg["createdAt"],
                    "feedback": sanitize_json_content(msg.get("feedback", ""))
                }
                messages.append(formatted_msg)
            except KeyError as ke:
                logger.error(f"Missing key in message: {ke}")
                logger.info(f"Message structure: {msg}")
                # Add a placeholder message if we couldn't format this message
                messages.append({
                    "id": msg.get("id", f"error-{len(messages)}"),
                    "role": msg.get("role", "system"),
                    "content": "Error loading this message",
                    "createdAt": msg.get("createdAt", datetime.utcnow().isoformat()),
                    "feedback": ""
                })
        
        # Test serializing the response to make sure it's valid JSON
        try:
            response_data = {"conversation_id": conversation_id, "messages": messages}
            
            # Test if the response can be properly JSON serialized
            response_str = json.dumps(response_data)
            
            # Test parsing it back to ensure it's valid
            json.loads(response_str)
            
            logger.info(f"Successfully validated response JSON for {len(messages)} messages")
            return jsonify(response_data), 200
        except Exception as json_e:
            logger.error(f"Error serializing response to JSON: {str(json_e)}")
            
            # Try to identify problematic messages
            for i, msg in enumerate(messages):
                try:
                    json.dumps(msg)
                except Exception as e:
                    logger.error(f"Message at index {i} is not JSON serializable: {str(e)}")
                    # Try to find which field is problematic
                    for k, v in msg.items():
                        try:
                            json.dumps({k: v})
                        except:
                            logger.error(f"Field '{k}' has invalid value: {repr(v)}")
            
            # Return filtered messages with problematic ones removed
            safe_messages = []
            for msg in messages:
                try:
                    json.dumps(msg)
                    safe_messages.append(msg)
                except:
                    pass
                    
            logger.info(f"Returning {len(safe_messages)} sanitized messages (removed {len(messages) - len(safe_messages)} problematic messages)")
            return jsonify({"conversation_id": conversation_id, "messages": safe_messages}), 200
    except Exception as e:
        logger.exception(f"Exception in /history/read: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation from cosmos
    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    ## update the title
    title = request_json.get("title", None)
    if not title:
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await current_app.cosmos_conversation_client.upsert_conversation(
        conversation
    )

    return jsonify(updated_conversation), 200


@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    await cosmos_db_ready.wait()
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # get conversations for user
    try:
        ## make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        conversations = await current_app.cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        # delete each conversation
        for conversation in conversations:
            ## delete the conversation messages from cosmos first
            deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )

            ## Now delete the conversation
            deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    await cosmos_db_ready.wait()
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos
        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    await cosmos_db_ready.wait()
    if not app_settings.chat_history:
        logger.error("CosmosDB is not configured - missing chat_history settings")
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        logger.info("Testing CosmosDB connection via /history/ensure endpoint")
        
        if not current_app.cosmos_conversation_client:
            logger.error("CosmosDB client is not initialized")
            return jsonify({"error": "CosmosDB client is not initialized"}), 500
            
        success, err = await current_app.cosmos_conversation_client.ensure()
        
        if not success:
            logger.error(f"CosmosDB connection test failed: {err}")
            if err:
                # Try to sanitize the error message to avoid JSON parsing issues
                sanitized_err = err.replace('\n', ' ').replace('\r', ' ')
                logger.info(f"Original error: {repr(err)}")
                logger.info(f"Sanitized error: {repr(sanitized_err)}")
                
                # Validate the error can be properly serialized to JSON
                try:
                    test_json = json.dumps({"error": sanitized_err})
                    json.loads(test_json)  # Verify it can be parsed back
                    return jsonify({"error": sanitized_err}), 422
                except Exception as json_e:
                    logger.error(f"Error response contains invalid JSON characters: {str(json_e)}")
                    return jsonify({"error": "Database connection error - see logs for details"}), 422
            
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        logger.info("CosmosDB connection test successful")
        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logger.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        
        # Try to sanitize the exception message before serializing
        try:
            sanitized_error = cosmos_exception.replace('\n', ' ').replace('\r', ' ')
            logger.info(f"Original exception: {repr(cosmos_exception)}")
            logger.info(f"Sanitized exception: {repr(sanitized_error)}")
            
            # Test if the sanitized message can be properly serialized
            test_json = json.dumps({"error": sanitized_error})
            json.loads(test_json)  # Verify it can be parsed back
            
            if "Invalid credentials" in sanitized_error:
                return jsonify({"error": sanitized_error}), 401
            elif "Invalid CosmosDB database name" in sanitized_error:
                return jsonify(
                    {
                        "error": f"{sanitized_error} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ), 422
            elif "Invalid CosmosDB container name" in sanitized_error:
                return jsonify(
                    {
                        "error": f"{sanitized_error}: {app_settings.chat_history.conversations_container}"
                    }
                ), 422
            else:
                return jsonify({"error": sanitized_error}), 500
        except Exception as json_e:
            logger.error(f"Error serializing exception to JSON: {str(json_e)}")
            return jsonify({"error": "Database error - see logs for details"}), 500


async def generate_title(conversation_messages) -> str:
    ## make sure the messages are sorted by _ts descending
    title_prompt = "Summarize the conversation so far into a 4-word or less title. Do not use any quotation marks or punctuation. Do not include any other commentary or description."

    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
    ]
    messages.append({"role": "user", "content": title_prompt})

    try:
        azure_openai_client = await init_openai_client()
        response = await azure_openai_client.chat.completions.create(
            model=app_settings.azure_openai.model, messages=messages, temperature=1, max_tokens=64
        )

        title = response.choices[0].message.content
        return title
    except Exception as e:
        logging.exception("Exception while generating title", e)
        return messages[-2]["content"]


@bp.route("/debug/cosmos", methods=["GET"])
async def debug_cosmos():
    """
    Debug endpoint to check CosmosDB connection and database structure.
    """
    if not app_settings.chat_history:
        logger.error("CosmosDB not configured - no chat_history settings found")
        return jsonify({
            "error": "CosmosDB not configured", 
            "settings": "missing"
        }), 404
        
    try:
        # Log environment settings
        logger.info("=== CosmosDB Environment Variables ===")
        cosmos_account = os.environ.get("AZURE_COSMOSDB_ACCOUNT", "Not set")
        cosmos_db = os.environ.get("AZURE_COSMOSDB_DATABASE", "Not set")
        cosmos_container = os.environ.get("AZURE_COSMOSDB_CONVERSATIONS_CONTAINER", "Not set")
        cosmos_key = os.environ.get("AZURE_COSMOSDB_ACCOUNT_KEY", "Not set")
        enable_feedback = os.environ.get("AZURE_COSMOSDB_ENABLE_FEEDBACK", "Not set")
        
        debug_info = {
            "environment": {
                "AZURE_COSMOSDB_ACCOUNT": cosmos_account,
                "AZURE_COSMOSDB_DATABASE": cosmos_db,
                "AZURE_COSMOSDB_CONVERSATIONS_CONTAINER": cosmos_container,
                "AZURE_COSMOSDB_ACCOUNT_KEY": "Present" if cosmos_key != "Not set" else "Not set",
                "AZURE_COSMOSDB_ENABLE_FEEDBACK": enable_feedback
            },
            "settings": {
                "account": app_settings.chat_history.account,
                "database": app_settings.chat_history.database,
                "container": app_settings.chat_history.conversations_container,
                "enable_feedback": app_settings.chat_history.enable_feedback,
                "account_key_present": app_settings.chat_history.account_key is not None
            }
        }
        
        logger.info(f"Environment variables: {debug_info['environment']}")
        logger.info(f"Settings: {debug_info['settings']}")
        
        if not current_app.cosmos_conversation_client:
            logger.error("CosmosDB client is not initialized")
            debug_info["client_status"] = "Not initialized"
            return jsonify(debug_info), 500
        
        # Check the connection and database/container setup
        success, message = await current_app.cosmos_conversation_client.ensure()
        debug_info["connection_test"] = {
            "success": success,
            "message": message
        }
        
        if not success:
            logger.error(f"CosmosDB connection failed: {message}")
            return jsonify(debug_info), 422
        
        # If connection is successful, check the database structure
        logger.info("Checking database and container structure...")
        debug_info["database_check"] = "Success"
        
        # Get database properties
        try:
            database_info = await current_app.cosmos_conversation_client.database_client.read()
            debug_info["database_info"] = {
                "id": database_info["id"],
                "rid": database_info.get("_rid", "N/A"),
                "self": database_info.get("_self", "N/A")
            }
            logger.info(f"Successfully connected to database: {database_info['id']}")
        except Exception as e:
            logger.error(f"Error accessing database: {str(e)}")
            debug_info["database_error"] = str(e)
            return jsonify(debug_info), 500
        
        # Get container properties
        try:
            container_info = await current_app.cosmos_conversation_client.container_client.read()
            debug_info["container_info"] = {
                "id": container_info["id"],
                "rid": container_info.get("_rid", "N/A"),
                "partition_key": container_info.get("partitionKey", {}).get("paths", ["N/A"])[0]
            }
            logger.info(f"Successfully connected to container: {container_info['id']}")
        except Exception as e:
            logger.error(f"Error accessing container: {str(e)}")
            debug_info["container_error"] = str(e)
            return jsonify(debug_info), 500
            
        return jsonify(debug_info), 200
    except Exception as e:
        logger.exception(f"Exception in /debug/cosmos: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/cosmos/create", methods=["GET"])
async def debug_cosmos_create():
    """
    Debug endpoint to attempt to create the CosmosDB database and container if they don't exist.
    """
    if not app_settings.chat_history:
        logger.error("CosmosDB not configured - no chat_history settings found")
        return jsonify({
            "error": "CosmosDB not configured", 
            "settings": "missing"
        }), 404
        
    try:
        cosmos_endpoint = f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
        logger.info(f"Attempting to connect to CosmosDB at {cosmos_endpoint}")
        
        if not app_settings.chat_history.account_key:
            logger.info("Using managed identity for authentication")
            async with DefaultAzureCredential() as cred:
                credential = cred
        else:
            logger.info("Using account key for authentication")
            credential = app_settings.chat_history.account_key
        
        # Create a new client directly to test database/container creation
        from azure.cosmos.aio import CosmosClient
        from azure.cosmos import exceptions, PartitionKey
        
        async with CosmosClient(cosmos_endpoint, credential=credential) as client:
            # Check if the database exists
            database_name = app_settings.chat_history.database
            container_name = app_settings.chat_history.conversations_container
            
            logger.info(f"Checking if database {database_name} exists")
            try:
                # Try to read the database to see if it exists
                database_client = client.get_database_client(database_name)
                await database_client.read()
                database_exists = True
                logger.info(f"Database {database_name} exists")
            except exceptions.CosmosResourceNotFoundError:
                # Database doesn't exist, create it
                database_exists = False
                logger.warning(f"Database {database_name} does not exist")
                
            # Create database if it doesn't exist
            if not database_exists:
                logger.info(f"Attempting to create database {database_name}")
                try:
                    database_client = await client.create_database(database_name)
                    logger.info(f"Successfully created database {database_name}")
                except Exception as e:
                    logger.error(f"Failed to create database: {str(e)}")
                    return jsonify({
                        "error": f"Failed to create database: {str(e)}",
                        "database": database_name
                    }), 500
            
            # Now check if the container exists
            try:
                container_client = database_client.get_container_client(container_name)
                await container_client.read()
                container_exists = True
                logger.info(f"Container {container_name} exists")
            except exceptions.CosmosResourceNotFoundError:
                container_exists = False
                logger.warning(f"Container {container_name} does not exist")
            
            # Create container if it doesn't exist
            if not container_exists:
                logger.info(f"Attempting to create container {container_name}")
                try:
                    container_client = await database_client.create_container(
                        id=container_name,
                        partition_key=PartitionKey(path="/id")
                    )
                    logger.info(f"Successfully created container {container_name}")
                except Exception as e:
                    logger.error(f"Failed to create container: {str(e)}")
                    return jsonify({
                        "error": f"Failed to create container: {str(e)}",
                        "container": container_name
                    }), 500
            
            # Test writing a sample document
            logger.info("Testing document creation")
            try:
                test_item = {
                    'id': f'test-{uuid.uuid4()}',
                    'type': 'test',
                    'userId': 'test-user',
                    'createdAt': datetime.utcnow().isoformat(),
                    'content': 'This is a test document'
                }
                
                created_item = await container_client.create_item(test_item)
                item_id = created_item['id']
                logger.info(f"Successfully created test document with id {item_id}")
                
                # Delete the test item
                await container_client.delete_item(item=item_id, partition_key=item_id)
                logger.info(f"Successfully deleted test document with id {item_id}")
            except Exception as e:
                logger.error(f"Failed to create/delete test document: {str(e)}")
                return jsonify({
                    "error": f"Failed to create/delete test document: {str(e)}"
                }), 500
                
            return jsonify({
                "message": "CosmosDB setup verified and test document created/deleted successfully",
                "database_existed": database_exists,
                "container_existed": container_exists,
                "status": "success"
            }), 200
                
    except Exception as e:
        logger.exception(f"Exception in /debug/cosmos/create: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/cosmos/permissions", methods=["GET"])
async def debug_cosmos_permissions():
    """
    Debug endpoint to test specific permissions to the CosmosDB database and container.
    """
    if not app_settings.chat_history:
        logger.error("CosmosDB not configured - no chat_history settings found")
        return jsonify({
            "error": "CosmosDB not configured", 
            "settings": "missing"
        }), 404
        
    try:
        cosmos_endpoint = f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
        logger.info(f"Testing permissions for CosmosDB at {cosmos_endpoint}")
        
        # Log auth method
        auth_type = "account_key" if app_settings.chat_history.account_key else "managed_identity"
        logger.info(f"Authentication method: {auth_type}")
        
        if not app_settings.chat_history.account_key:
            logger.info("Using managed identity for authentication")
            async with DefaultAzureCredential() as cred:
                credential = cred
        else:
            logger.info("Using account key for authentication")
            credential = app_settings.chat_history.account_key
        
        # Try a direct test with detailed errors
        from azure.cosmos.aio import CosmosClient
        from azure.cosmos import exceptions
        
        results = {
            "account": app_settings.chat_history.account,
            "database": app_settings.chat_history.database,
            "container": app_settings.chat_history.conversations_container,
            "auth_type": auth_type,
            "tests": {}
        }
        
        # Test account access
        try:
            logger.info("Testing account connection...")
            async with CosmosClient(cosmos_endpoint, credential=credential) as client:
                logger.info("Successfully connected to CosmosDB account")
                results["tests"]["account_connection"] = "Success"
                
                # Try to list databases
                logger.info("Testing ability to list databases...")
                database_list = []
                async for database in client.list_databases():
                    database_list.append(database["id"])
                
                logger.info(f"Found databases: {database_list}")
                results["tests"]["list_databases"] = {
                    "status": "Success",
                    "found": database_list
                }
                
                # Try to access the specific database
                try:
                    database_name = app_settings.chat_history.database
                    logger.info(f"Testing database access: {database_name}")
                    database = client.get_database_client(database_name)
                    await database.read()
                    logger.info(f"Successfully accessed database '{database_name}'")
                    results["tests"]["database_access"] = "Success"
                    
                    # Try to list containers
                    try:
                        logger.info("Testing ability to list containers...")
                        container_list = []
                        async for container in database.list_containers():
                            container_list.append(container["id"])
                        
                        logger.info(f"Found containers: {container_list}")
                        results["tests"]["list_containers"] = {
                            "status": "Success",
                            "found": container_list
                        }
                        
                        # Try to access the container
                        try:
                            container_name = app_settings.chat_history.conversations_container
                            logger.info(f"Testing container access: {container_name}")
                            container = database.get_container_client(container_name)
                            await container.read()
                            logger.info(f"Successfully accessed container '{container_name}'")
                            results["tests"]["container_access"] = "Success"
                            
                            # Try to query container
                            try:
                                logger.info("Testing ability to query container...")
                                items = []
                                async for item in container.query_items(
                                    query="SELECT TOP 1 * FROM c"
                                ):
                                    items.append(item)
                                
                                if items:
                                    logger.info(f"Successfully queried container, found {len(items)} items")
                                    sample_keys = list(items[0].keys()) if items else []
                                    results["tests"]["query_container"] = {
                                        "status": "Success",
                                        "found_items": len(items),
                                        "sample_keys": sample_keys
                                    }
                                else:
                                    logger.info("Container is empty but query was successful")
                                    results["tests"]["query_container"] = {
                                        "status": "Success", 
                                        "found_items": 0
                                    }
                                
                                # Try to create a test item
                                try:
                                    logger.info("Testing write permission...")
                                    test_item = {
                                        "id": f"test-{uuid.uuid4()}",
                                        "type": "test",
                                        "userId": "test-user",
                                        "createdAt": datetime.utcnow().isoformat()
                                    }
                                    
                                    created = await container.create_item(body=test_item)
                                    logger.info(f"Successfully created test item with id: {created['id']}")
                                    results["tests"]["write_permission"] = "Success"
                                    
                                    # Clean up test item
                                    await container.delete_item(item=created['id'], partition_key=created['id'])
                                    logger.info(f"Successfully deleted test item")
                                except Exception as e:
                                    logger.error(f"Error testing write permission: {str(e)}")
                                    results["tests"]["write_permission"] = {
                                        "status": "Failed",
                                        "error": str(e)
                                    }
                            except Exception as e:
                                logger.error(f"Error querying container: {str(e)}")
                                results["tests"]["query_container"] = {
                                    "status": "Failed",
                                    "error": str(e)
                                }
                        except Exception as e:
                            logger.error(f"Error accessing container: {str(e)}")
                            results["tests"]["container_access"] = {
                                "status": "Failed",
                                "error": str(e)
                            }
                    except Exception as e:
                        logger.error(f"Error listing containers: {str(e)}")
                        results["tests"]["list_containers"] = {
                            "status": "Failed",
                            "error": str(e)
                        }
                except exceptions.CosmosResourceNotFoundError:
                    logger.error(f"Database '{database_name}' not found")
                    results["tests"]["database_access"] = {
                        "status": "Failed",
                        "error": f"Database '{database_name}' not found"
                    }
                except exceptions.CosmosHttpResponseError as e:
                    logger.error(f"Error accessing database: {str(e)}")
                    if e.status_code == 403:
                        results["tests"]["database_access"] = {
                            "status": "Failed",
                            "error": "Permission denied (403 Forbidden)"
                        }
                    else:
                        results["tests"]["database_access"] = {
                            "status": "Failed",
                            "error": f"HTTP Error {e.status_code}: {str(e)}"
                        }
                except Exception as e:
                    logger.error(f"Error accessing database: {str(e)}")
                    results["tests"]["database_access"] = {
                        "status": "Failed",
                        "error": str(e)
                    }
        except exceptions.CosmosHttpResponseError as e:
            if e.status_code == 401:
                logger.error(f"Authentication failed: {str(e)}")
                results["tests"]["account_connection"] = {
                    "status": "Failed",
                    "error": "Authentication failed (401 Unauthorized)"
                }
            else:
                logger.error(f"HTTP error connecting to CosmosDB: {str(e)}")
                results["tests"]["account_connection"] = {
                    "status": "Failed",
                    "error": f"HTTP Error {e.status_code}: {str(e)}"
                }
        except Exception as e:
            logger.error(f"Error connecting to CosmosDB: {str(e)}")
            results["tests"]["account_connection"] = {
                "status": "Failed",
                "error": str(e)
            }
            
        return jsonify(results), 200
                
    except Exception as e:
        logger.exception(f"Exception in /debug/cosmos/permissions: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/feedback", methods=["GET"])
async def debug_feedback():
    """
    Debug endpoint to retrieve all feedback entries for the current user.
    """
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    
    try:
        if not current_app.cosmos_conversation_client:
            logger.error("CosmosDB client is not initialized")
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500
        
        # Query for all messages with feedback
        feedback_query = """
        SELECT c.id, c.feedback, c.content, c.role, c.conversationId, c.createdAt, c.updatedAt 
        FROM c 
        WHERE c.userId = @userId 
        AND c.type = 'message' 
        AND IS_DEFINED(c.feedback) 
        AND c.feedback <> ''
        ORDER BY c.updatedAt DESC
        """
        
        parameters = [
            {
                'name': '@userId',
                'value': user_id
            }
        ]
        
        feedback_items = []
        # Simplified query without query_options or partition_key
        async for item in current_app.cosmos_conversation_client.container_client.query_items(
            query=feedback_query,
            parameters=parameters
        ):
            # Truncate content to avoid overwhelming the response
            if 'content' in item and item['content']:
                item['content'] = item['content'][:100] + "..." if len(item['content']) > 100 else item['content']
            
            feedback_items.append(item)
        
        logger.info(f"Found {len(feedback_items)} feedback items for user {user_id}")
        
        # For each feedback item, try to find the associated user query
        for item in feedback_items:
            conversation_id = item.get('conversationId')
            if conversation_id:
                # Find the most recent user message before this assistant message
                user_query_search = """
                SELECT TOP 1 c.content 
                FROM c 
                WHERE c.conversationId = @conversationId 
                AND c.userId = @userId 
                AND c.type = 'message' 
                AND c.role = 'user' 
                AND c.createdAt < @createdAt 
                ORDER BY c.createdAt DESC
                """
                
                query_params = [
                    {
                        'name': '@conversationId',
                        'value': conversation_id
                    },
                    {
                        'name': '@userId',
                        'value': user_id
                    },
                    {
                        'name': '@createdAt',
                        'value': item.get('createdAt')
                    }
                ]
                
                user_queries = []
                # Simplified query without query_options or partition_key
                async for query_item in current_app.cosmos_conversation_client.container_client.query_items(
                    query=user_query_search,
                    parameters=query_params
                ):
                    if 'content' in query_item:
                        content = query_item['content']
                        user_queries.append(content[:100] + "..." if len(content) > 100 else content)
                
                if user_queries:
                    item['user_query'] = user_queries[0]
                else:
                    item['user_query'] = "No associated user query found"
        
        return jsonify({
            "feedback_count": len(feedback_items),
            "feedback_items": feedback_items
        }), 200
        
    except Exception as e:
        logger.exception(f"Exception in /debug/feedback: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/similar-questions", methods=["GET"])
async def similar_questions():
    query = request.args.get("query", "")
    logger.info(f"Similar questions endpoint called with query: '{query}'")
    
    if not query:
        logger.info("Empty query received, returning empty results")
        return jsonify([])

    try:
        # Wait for Cosmos DB to be ready
        logger.info("Waiting for Cosmos DB to be ready...")
        await cosmos_db_ready.wait()
        
        if not current_app.cosmos_conversation_client:
            logger.error("CosmosDB client is not initialized")
            return jsonify({"error": "Database connection not available"}), 500
            
        cosmos_client = current_app.cosmos_conversation_client
        logger.info(f"CosmosDB client obtained, container: {cosmos_client.container_name}")

        # Get all user questions from Cosmos DB
        questions = []
        try:
            logger.info("Executing query to find user messages...")
            # Remove the problematic parameter
            async for item in cosmos_client.container_client.query_items(
                query="SELECT c.id, c.content FROM c WHERE c.type='message' AND c.role='user'"
            ):
                questions.append(item)
            
            logger.info(f"Found {len(questions)} total user questions")
        except Exception as query_error:
            logger.error(f"Error querying CosmosDB: {str(query_error)}")
            return jsonify({"error": f"Database query error: {str(query_error)}"}), 500

        # Find similar questions (simple substring match for now)
        try:
            def is_similar(q):
                if not q.get('content'):
                    return False
                return query.lower() in q['content'].lower()
                
            similar = [q for q in questions if is_similar(q)]
            logger.info(f"Found {len(similar)} similar questions matching query")
            
            # Return up to 3 similar questions (1-3, not always 3)
            result = [{"id": q["id"], "text": q["content"]} for q in similar[:3]]
            logger.info(f"Returning {len(result)} follow-up questions: {result}")
            
            return jsonify(result)
        except Exception as proc_error:
            logger.error(f"Error processing results: {str(proc_error)}")
            return jsonify({"error": f"Error processing results: {str(proc_error)}"}), 500
            
    except Exception as e:
        logger.exception(f"Unhandled exception in similar_questions: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/answer/<message_id>", methods=["GET"])
async def get_answer_by_id(message_id):
    """
    Retrieves an assistant answer from CosmosDB by its message ID.
    """
    logger.info(f"Answer retrieval endpoint called for message ID: {message_id}")
    
    try:
        # Wait for Cosmos DB to be ready
        await cosmos_db_ready.wait()
        
        if not current_app.cosmos_conversation_client:
            logger.error("CosmosDB client is not initialized")
            return jsonify({"error": "Database connection not available"}), 500
            
        cosmos_client = current_app.cosmos_conversation_client
        
        # Try to get the message directly
        try:
            # First try to query for the message
            query = f"SELECT * FROM c WHERE c.id = '{message_id}'"
            message = None
            
            async for item in cosmos_client.container_client.query_items(query=query):
                message = item
                break
                
            if not message:
                logger.error(f"Message with ID {message_id} not found")
                return jsonify({"error": "Message not found"}), 404
                
            # Check if this is a user message
            if message.get("role") == "user":
                # Find the response to this user message
                conversation_id = message.get("conversationId")
                if not conversation_id:
                    logger.error(f"Message {message_id} has no conversation ID")
                    return jsonify({"error": "No conversation ID associated with message"}), 404
                    
                # Find the assistant response that follows this user message
                query = f"""
                SELECT * FROM c 
                WHERE c.conversationId = '{conversation_id}' 
                AND c.role = 'assistant' 
                AND c.createdAt > '{message.get("createdAt")}'
                ORDER BY c.createdAt
                """
                
                assistant_message = None
                async for item in cosmos_client.container_client.query_items(query=query):
                    assistant_message = item
                    break
                    
                if assistant_message:
                    return jsonify({"answer": assistant_message.get("content", ""), "id": assistant_message.get("id")})
                else:
                    return jsonify({"answer": "No answer found for this question", "id": ""}), 404
            else:
                # This is already an assistant message, return it directly
                return jsonify({"answer": message.get("content", ""), "id": message_id})
                
        except Exception as query_error:
            logger.error(f"Error querying for message {message_id}: {str(query_error)}")
            return jsonify({"error": f"Database query error: {str(query_error)}"}), 500
            
    except Exception as e:
        logger.exception(f"Unhandled exception in get_answer_by_id: {str(e)}")
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/data-files", methods=["GET"])
async def list_data_files():
    """Debug endpoint to list all files in the data directory."""
    import os
    
    try:
        data_dir = "data"
        # Get all files in the data directory, recursively
        files = []
        for root, dirs, filenames in os.walk(data_dir):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, data_dir)
                files.append({
                    "filename": filename,
                    "path": rel_path,
                    "full_path": file_path,
                    "size": os.path.getsize(file_path),
                    "type": os.path.splitext(filename)[1].lower(),
                })
        
        return jsonify({
            "data_dir": os.path.abspath(data_dir),
            "file_count": len(files),
            "files": files
        }), 200
    except Exception as e:
        logging.exception("Error listing data files")
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/check-file", methods=["GET"])
async def check_file_exists():
    """Debug endpoint to check if a specific file exists."""
    import os
    
    file_path = request.args.get("path", "")
    if not file_path:
        return jsonify({"error": "No file path provided. Use ?path=filename.pdf"}), 400
    
    # Make sure we don't allow path traversal
    if ".." in file_path:
        return jsonify({"error": "Invalid file path"}), 400
    
    # Check if path is absolute or relative
    if os.path.isabs(file_path):
        full_path = file_path
    else:
        # Prepend data directory if relative path
        if not file_path.startswith("data/"):
            full_path = os.path.join("data", file_path)
        else:
            full_path = file_path
    
    try:
        exists = os.path.exists(full_path)
        
        result = {
            "file_path": file_path,
            "full_path": full_path,
            "exists": exists
        }
        
        if exists:
            result.update({
                "size": os.path.getsize(full_path),
                "is_file": os.path.isfile(full_path),
                "is_dir": os.path.isdir(full_path),
                "type": os.path.splitext(full_path)[1].lower() if os.path.isfile(full_path) else None
            })
        
        return jsonify(result), 200
    except Exception as e:
        logging.exception(f"Error checking file: {file_path}")
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/pdf-info", methods=["GET"])
async def debug_pdf_info():
    """
    Debug endpoint to list all PDF files in data and site_pdfs directories.
    Returns detailed information about each PDF file for debugging.
    """
    import os
    import time
    import hashlib
    
    logging.info("PDF info debug endpoint called")
    
    try:
        result = {
            "timestamp": time.time(),
            "data_directory": {},
            "site_pdfs_directory": {}
        }
        
        # Check data directory
        data_dir = "data"
        if os.path.exists(data_dir) and os.path.isdir(data_dir):
            logging.info(f"Checking {data_dir} directory for PDFs")
            data_files = []
            
            for filename in os.listdir(data_dir):
                if filename.lower().endswith('.pdf'):
                    file_path = os.path.join(data_dir, filename)
                    file_size = os.path.getsize(file_path)
                    file_mtime = os.path.getmtime(file_path)
                    
                    # Calculate file hash for first 1024 bytes (for quick fingerprinting)
                    file_hash = ""
                    try:
                        with open(file_path, 'rb') as f:
                            file_hash = hashlib.md5(f.read(1024)).hexdigest()
                    except Exception as e:
                        file_hash = f"Error: {str(e)}"
                    
                    data_files.append({
                        "filename": filename,
                        "path": file_path,
                        "size_bytes": file_size,
                        "modified_time": file_mtime,
                        "modified_time_str": time.ctime(file_mtime),
                        "hash_first_1k": file_hash
                    })
            
            result["data_directory"] = {
                "exists": True,
                "path": os.path.abspath(data_dir),
                "file_count": len(data_files),
                "files": data_files
            }
        else:
            result["data_directory"] = {
                "exists": False,
                "path": os.path.abspath(data_dir) if os.path.exists(data_dir) else "Not found"
            }
        
        # Check site_pdfs directory
        site_pdfs_dir = "site_pdfs"
        if os.path.exists(site_pdfs_dir) and os.path.isdir(site_pdfs_dir):
            logging.info(f"Checking {site_pdfs_dir} directory for PDFs")
            site_pdfs_files = []
            
            for filename in os.listdir(site_pdfs_dir):
                if filename.lower().endswith('.pdf'):
                    file_path = os.path.join(site_pdfs_dir, filename)
                    file_size = os.path.getsize(file_path)
                    file_mtime = os.path.getmtime(file_path)
                    
                    # Calculate file hash for first 1024 bytes
                    file_hash = ""
                    try:
                        with open(file_path, 'rb') as f:
                            file_hash = hashlib.md5(f.read(1024)).hexdigest()
                    except Exception as e:
                        file_hash = f"Error: {str(e)}"
                    
                    site_pdfs_files.append({
                        "filename": filename,
                        "path": file_path,
                        "size_bytes": file_size,
                        "modified_time": file_mtime,
                        "modified_time_str": time.ctime(file_mtime),
                        "hash_first_1k": file_hash
                    })
            
            result["site_pdfs_directory"] = {
                "exists": True,
                "path": os.path.abspath(site_pdfs_dir),
                "file_count": len(site_pdfs_files),
                "files": site_pdfs_files
            }
        else:
            result["site_pdfs_directory"] = {
                "exists": False,
                "path": os.path.abspath(site_pdfs_dir) if os.path.exists(site_pdfs_dir) else "Not found"
            }
        
        # Check if the directories were created
        if not result["data_directory"].get("exists"):
            logging.info("Creating data directory as it doesn't exist")
            os.makedirs(data_dir, exist_ok=True)
            result["data_directory"]["created_now"] = True
            
        if not result["site_pdfs_directory"].get("exists"):
            logging.info("Creating site_pdfs directory as it doesn't exist")
            os.makedirs(site_pdfs_dir, exist_ok=True)
            result["site_pdfs_directory"]["created_now"] = True
            
        logging.info(f"PDF info debug completed. Found {result['data_directory'].get('file_count', 0)} PDFs in data dir and {result['site_pdfs_directory'].get('file_count', 0)} PDFs in site_pdfs dir")
        return jsonify(result), 200
        
    except Exception as e:
        logging.exception(f"Exception in /debug/pdf-info: {str(e)}")
        return jsonify({"error": str(e)}), 500


app = create_app()
