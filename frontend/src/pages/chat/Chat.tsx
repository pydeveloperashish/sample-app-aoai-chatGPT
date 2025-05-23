import { useRef, useState, useEffect, useContext, useLayoutEffect, useCallback } from 'react'
import { CommandBarButton, IconButton, Dialog, DialogType, Stack, Button } from '@fluentui/react'
import { SquareRegular, ShieldLockRegular, ErrorCircleRegular } from '@fluentui/react-icons'
import { FontIcon } from '@fluentui/react'

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import uuid from 'react-uuid'
import { isEmpty } from 'lodash'
import DOMPurify from 'dompurify'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { nord } from 'react-syntax-highlighter/dist/esm/styles/prism'

import styles from './Chat.module.css'
import Contoso from '../../assets/Contoso.svg'
import { XSSAllowTags } from '../../constants/sanatizeAllowables'

import {
  ChatMessage,
  ConversationRequest,
  conversationApi,
  Citation,
  ToolMessageContent,
  AzureSqlServerExecResults,
  ChatResponse,
  getUserInfo,
  Conversation,
  historyGenerate,
  historyUpdate,
  historyClear,
  ChatHistoryLoadingState,
  CosmosDBStatus,
  ErrorMessage,
  ExecResults,
} from "../../api";
import { Answer } from "../../components/Answer";
import { QuestionInput } from "../../components/QuestionInput";
import { ChatHistoryPanel } from "../../components/ChatHistory/ChatHistoryPanel";
import { AppStateContext } from "../../state/AppProvider";
import { useBoolean } from "@fluentui/react-hooks";

const enum messageStatus {
  NotRunning = 'Not Running',
  Processing = 'Processing',
  Done = 'Done'
}

const Chat = () => {
  const appStateContext = useContext(AppStateContext)
  const ui = appStateContext?.state.frontendSettings?.ui
  const AUTH_ENABLED = appStateContext?.state.frontendSettings?.auth_enabled
  const chatMessageStreamEnd = useRef<HTMLDivElement | null>(null)
  const [isLoading, setIsLoading] = useState<boolean>(false)
  const [showLoadingMessage, setShowLoadingMessage] = useState<boolean>(false)
  const [isCitationPanelOpen, setIsCitationPanelOpen] = useState<boolean>(false)
  const [isIntentsPanelOpen, setIsIntentsPanelOpen] = useState<boolean>(false)
  const abortFuncs = useRef([] as AbortController[])
  const [showAuthMessage, setShowAuthMessage] = useState<boolean | undefined>()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [execResults, setExecResults] = useState<ExecResults[]>([])
  const [processMessages, setProcessMessages] = useState<messageStatus>(messageStatus.NotRunning)
  const [clearingChat, setClearingChat] = useState<boolean>(false)
  const [hideErrorDialog, { toggle: toggleErrorDialog }] = useBoolean(true)
  const [errorMsg, setErrorMsg] = useState<ErrorMessage | null>()
  const [logo, setLogo] = useState('')
  const [answerId, setAnswerId] = useState<string>('')
  const citationContentRef = useRef<HTMLDivElement | null>(null)
  const [qaPairs, setQaPairs] = useState<{ question: string, answer: string, followUps: { id: string, text: string }[] }[]>([])

  const errorDialogContentProps = {
    type: DialogType.close,
    title: errorMsg?.title,
    closeButtonAriaLabel: 'Close',
    subText: errorMsg?.subtitle
  }

  const modalProps = {
    titleAriaId: 'labelId',
    subtitleAriaId: 'subTextId',
    isBlocking: true,
    styles: { main: { maxWidth: 450 } }
  }

  const [ASSISTANT, TOOL, ERROR] = ['assistant', 'tool', 'error']
  const NO_CONTENT_ERROR = 'No content in messages object.'

  useEffect(() => {
    if (
      appStateContext?.state.isCosmosDBAvailable?.status !== CosmosDBStatus.Working &&
      appStateContext?.state.isCosmosDBAvailable?.status !== CosmosDBStatus.NotConfigured &&
      appStateContext?.state.chatHistoryLoadingState === ChatHistoryLoadingState.Fail &&
      hideErrorDialog
    ) {
      let subtitle = `${appStateContext.state.isCosmosDBAvailable.status}. Please contact the site administrator.`
      setErrorMsg({
        title: 'Chat history is not enabled',
        subtitle: subtitle
      })
      toggleErrorDialog()
    }
  }, [appStateContext?.state.isCosmosDBAvailable])

  const handleErrorDialogClose = () => {
    toggleErrorDialog()
    setTimeout(() => {
      setErrorMsg(null)
    }, 500)
  }

  useEffect(() => {
    if (!appStateContext?.state.isLoading) {
      setLogo(ui?.chat_logo || ui?.logo || Contoso)
    }
  }, [appStateContext?.state.isLoading])

  useEffect(() => {
    setIsLoading(appStateContext?.state.chatHistoryLoadingState === ChatHistoryLoadingState.Loading)
  }, [appStateContext?.state.chatHistoryLoadingState])

  const getUserInfoList = async () => {
    if (!AUTH_ENABLED) {
      setShowAuthMessage(false)
      return
    }
    const userInfoList = await getUserInfo()
    if (userInfoList.length === 0 && window.location.hostname !== '127.0.0.1') {
      setShowAuthMessage(true)
    } else {
      setShowAuthMessage(false)
    }
  }

  let assistantMessage = {} as ChatMessage
  let toolMessage = {} as ChatMessage
  let assistantContent = ''

  useEffect(() => parseExecResults(execResults), [execResults])

  const parseExecResults = (exec_results_: any): void => {
    if (exec_results_ == undefined) return
    const exec_results = exec_results_.length === 2 ? exec_results_ : exec_results_.splice(2)
    appStateContext?.dispatch({ type: 'SET_ANSWER_EXEC_RESULT', payload: { answerId: answerId, exec_result: exec_results } })
  }

  const processResultMessage = (resultMessage: ChatMessage, userMessage: ChatMessage, conversationId?: string) => {
    if (typeof resultMessage.content === "string" && resultMessage.content.includes('all_exec_results')) {
      const parsedExecResults = JSON.parse(resultMessage.content) as AzureSqlServerExecResults
      setExecResults(parsedExecResults.all_exec_results)
      assistantMessage.context = JSON.stringify({
        all_exec_results: parsedExecResults.all_exec_results
      })
    }

    if (resultMessage.role === ASSISTANT) {
      setAnswerId(resultMessage.id)
      assistantContent += resultMessage.content
      assistantMessage = { ...assistantMessage, ...resultMessage }
      assistantMessage.content = assistantContent

      if (resultMessage.context) {
        toolMessage = {
          id: uuid(),
          role: TOOL,
          content: resultMessage.context,
          date: new Date().toISOString()
        }
      }
    }

    if (resultMessage.role === TOOL) toolMessage = resultMessage

    if (!conversationId) {
      isEmpty(toolMessage)
        ? setMessages([...messages, userMessage, assistantMessage])
        : setMessages([...messages, userMessage, toolMessage, assistantMessage])
    } else {
      isEmpty(toolMessage)
        ? setMessages([...messages, assistantMessage])
        : setMessages([...messages, toolMessage, assistantMessage])
    }
  }

  // Helper to always get a string from question
  function extractQuestionText(question: any): string {
    if (typeof question === 'string') return question;
    if (Array.isArray(question) && question[0]?.text) return question[0].text;
    return '';
  }

  const makeApiRequestWithoutCosmosDB = async (question: ChatMessage["content"], conversationId?: string) => {
    setIsLoading(true)
    setShowLoadingMessage(true)
    const abortController = new AbortController()
    abortFuncs.current.unshift(abortController)

    const questionText = extractQuestionText(question);
    const questionContent = typeof question === 'string' ? questionText : [{ type: "text", text: questionText }]

    const userMessage: ChatMessage = {
      id: uuid(),
      role: 'user',
      content: questionContent as string,
      date: new Date().toISOString()
    }

    let conversation: Conversation | null | undefined
    if (!conversationId) {
      conversation = {
        id: conversationId ?? uuid(),
        title: questionText,
        messages: [userMessage],
        date: new Date().toISOString()
      }
    } else {
      conversation = appStateContext?.state?.currentChat
      if (!conversation) {
        console.error('Conversation not found.')
        setIsLoading(false)
        setShowLoadingMessage(false)
        abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
        return
      } else {
        conversation.messages.push(userMessage)
      }
    }

    appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: conversation })
    setMessages(conversation.messages)

    const request: ConversationRequest = {
      messages: [...conversation.messages.filter(answer => answer.role !== ERROR)]
    }

    let result = {} as ChatResponse
    try {
      const response = await conversationApi(request, abortController.signal)
      if (response?.body) {
        const reader = response.body.getReader()

        let runningText = ''
        while (true) {
          setProcessMessages(messageStatus.Processing)
          const { done, value } = await reader.read()
          if (done) break

          var text = new TextDecoder('utf-8').decode(value)
          const objects = text.split('\n')
          objects.forEach(obj => {
            try {
              if (obj !== '' && obj !== '{}') {
                runningText += obj
                result = JSON.parse(runningText)
                if (result.choices?.length > 0) {
                  result.choices[0].messages.forEach(msg => {
                    msg.id = result.id
                    msg.date = new Date().toISOString()
                  })
                  if (result.choices[0].messages?.some(m => m.role === ASSISTANT)) {
                    setShowLoadingMessage(false)
                  }
                  result.choices[0].messages.forEach(resultObj => {
                    processResultMessage(resultObj, userMessage, conversationId)
                  })
                } else if (result.error) {
                  throw Error(result.error)
                }
                runningText = ''
              }
            } catch (e) {
              if (!(e instanceof SyntaxError)) {
                console.error(e)
                throw e
              } else {
                console.log('Incomplete message. Continuing...')
              }
            }
          })
        }
        conversation.messages.push(toolMessage, assistantMessage)
        appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: conversation })
        setMessages([...messages, toolMessage, assistantMessage])
      }
    } catch (e) {
      if (!abortController.signal.aborted) {
        let errorMessage =
          'An error occurred. Please try again. If the problem persists, please contact the site administrator.'
        if (result.error?.message) {
          errorMessage = result.error.message
        } else if (typeof result.error === 'string') {
          errorMessage = result.error
        }

        errorMessage = parseErrorMessage(errorMessage)

        let errorChatMsg: ChatMessage = {
          id: uuid(),
          role: ERROR,
          content: errorMessage,
          date: new Date().toISOString()
        }
        conversation.messages.push(errorChatMsg)
        appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: conversation })
        setMessages([...messages, errorChatMsg])
      } else {
        setMessages([...messages, userMessage])
      }
    } finally {
      setIsLoading(false)
      setShowLoadingMessage(false)
      abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
      setProcessMessages(messageStatus.Done)
    }

    // After answer is received and setMessages is called:
    // Fetch follow-up questions
    const followUps = await fetchFollowUps(questionText)
    setQaPairs(prev => [
      ...prev,
      {
        question: questionText,
        answer: typeof assistantMessage.content === 'string' ? assistantMessage.content : '',
        followUps
      }
    ])

    return abortController.abort()
  }

  const makeApiRequestWithCosmosDB = async (question: ChatMessage["content"], conversationId?: string) => {
    setIsLoading(true)
    setShowLoadingMessage(true)
    const abortController = new AbortController()
    abortFuncs.current.unshift(abortController)
    const questionText = extractQuestionText(question);
    const questionContent = typeof question === 'string' ? questionText : [{ type: "text", text: questionText }]

    const userMessage: ChatMessage = {
      id: uuid(),
      role: 'user',
      content: questionContent as string,
      date: new Date().toISOString()
    }

    let request: ConversationRequest
    let conversation
    if (conversationId) {
      conversation = appStateContext?.state?.chatHistory?.find(conv => conv.id === conversationId)
      if (!conversation) {
        console.error('Conversation not found.')
        setIsLoading(false)
        setShowLoadingMessage(false)
        abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
        return
      } else {
        conversation.messages.push(userMessage)
        request = {
          messages: [...conversation.messages.filter(answer => answer.role !== ERROR)]
        }
      }
    } else {
      request = {
        messages: [userMessage].filter(answer => answer.role !== ERROR)
      }
      setMessages(request.messages)
    }
    let result = {} as ChatResponse
    var errorResponseMessage = 'Please try again. If the problem persists, please contact the site administrator.'
    try {
      const response = conversationId
        ? await historyGenerate(request, abortController.signal, conversationId)
        : await historyGenerate(request, abortController.signal)
      if (!response?.ok) {
        const responseJson = await response.json()
        errorResponseMessage =
          responseJson.error === undefined ? errorResponseMessage : parseErrorMessage(responseJson.error)
        let errorChatMsg: ChatMessage = {
          id: uuid(),
          role: ERROR,
          content: `There was an error generating a response. Chat history can't be saved at this time. ${errorResponseMessage}`,
          date: new Date().toISOString()
        }
        let resultConversation
        if (conversationId) {
          resultConversation = appStateContext?.state?.chatHistory?.find(conv => conv.id === conversationId)
          if (!resultConversation) {
            console.error('Conversation not found.')
            setIsLoading(false)
            setShowLoadingMessage(false)
            abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
            return
          }
          resultConversation.messages.push(errorChatMsg)
        } else {
          setMessages([...messages, userMessage, errorChatMsg])
          setIsLoading(false)
          setShowLoadingMessage(false)
          abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
          return
        }
        appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: resultConversation })
        setMessages([...resultConversation.messages])
        return
      }
      if (response?.body) {
        const reader = response.body.getReader()

        let runningText = ''
        while (true) {
          setProcessMessages(messageStatus.Processing)
          const { done, value } = await reader.read()
          if (done) break

          var text = new TextDecoder('utf-8').decode(value)
          const objects = text.split('\n')
          objects.forEach(obj => {
            try {
              if (obj !== '' && obj !== '{}') {
                runningText += obj
                result = JSON.parse(runningText)
                if (!result.choices?.[0]?.messages?.[0].content) {
                  errorResponseMessage = NO_CONTENT_ERROR
                  throw Error()
                }
                if (result.choices?.length > 0) {
                  result.choices[0].messages.forEach(msg => {
                    msg.id = result.id
                    msg.date = new Date().toISOString()
                  })
                  if (result.choices[0].messages?.some(m => m.role === ASSISTANT)) {
                    setShowLoadingMessage(false)
                  }
                  result.choices[0].messages.forEach(resultObj => {
                    processResultMessage(resultObj, userMessage, conversationId)
                  })
                }
                runningText = ''
              } else if (result.error) {
                throw Error(result.error)
              }
            } catch (e) {
              if (!(e instanceof SyntaxError)) {
                console.error(e)
                throw e
              } else {
                console.log('Incomplete message. Continuing...')
              }
            }
          })
        }

        let resultConversation
        if (conversationId) {
          resultConversation = appStateContext?.state?.chatHistory?.find(conv => conv.id === conversationId)
          if (!resultConversation) {
            console.error('Conversation not found.')
            setIsLoading(false)
            setShowLoadingMessage(false)
            abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
            return
          }
          isEmpty(toolMessage)
            ? resultConversation.messages.push(assistantMessage)
            : resultConversation.messages.push(toolMessage, assistantMessage)
        } else {
          resultConversation = {
            id: result.history_metadata.conversation_id,
            title: result.history_metadata.title,
            messages: [userMessage],
            date: result.history_metadata.date
          }
          isEmpty(toolMessage)
            ? resultConversation.messages.push(assistantMessage)
            : resultConversation.messages.push(toolMessage, assistantMessage)
        }
        if (!resultConversation) {
          setIsLoading(false)
          setShowLoadingMessage(false)
          abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
          return
        }
        appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: resultConversation })
        isEmpty(toolMessage)
          ? setMessages([...messages, assistantMessage])
          : setMessages([...messages, toolMessage, assistantMessage])
      }
    } catch (e) {
      if (!abortController.signal.aborted) {
        let errorMessage = `An error occurred. ${errorResponseMessage}`
        if (result.error?.message) {
          errorMessage = result.error.message
        } else if (typeof result.error === 'string') {
          errorMessage = result.error
        }

        errorMessage = parseErrorMessage(errorMessage)

        let errorChatMsg: ChatMessage = {
          id: uuid(),
          role: ERROR,
          content: errorMessage,
          date: new Date().toISOString()
        }
        let resultConversation
        if (conversationId) {
          resultConversation = appStateContext?.state?.chatHistory?.find(conv => conv.id === conversationId)
          if (!resultConversation) {
            console.error('Conversation not found.')
            setIsLoading(false)
            setShowLoadingMessage(false)
            abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
            return
          }
          resultConversation.messages.push(errorChatMsg)
        } else {
          if (!result.history_metadata) {
            console.error('Error retrieving data.', result)
            let errorChatMsg: ChatMessage = {
              id: uuid(),
              role: ERROR,
              content: errorMessage,
              date: new Date().toISOString()
            }
            setMessages([...messages, userMessage, errorChatMsg])
            setIsLoading(false)
            setShowLoadingMessage(false)
            abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
            return
          }
          resultConversation = {
            id: result.history_metadata.conversation_id,
            title: result.history_metadata.title,
            messages: [userMessage],
            date: result.history_metadata.date
          }
          resultConversation.messages.push(errorChatMsg)
        }
        if (!resultConversation) {
          setIsLoading(false)
          setShowLoadingMessage(false)
          abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
          return
        }
        appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: resultConversation })
        setMessages([...messages, errorChatMsg])
      } else {
        setMessages([...messages, userMessage])
      }
    } finally {
      setIsLoading(false)
      setShowLoadingMessage(false)
      abortFuncs.current = abortFuncs.current.filter(a => a !== abortController)
      setProcessMessages(messageStatus.Done)
    }

    // After answer is received and setMessages is called:
    // Fetch follow-up questions
    const followUps = await fetchFollowUps(questionText)
    setQaPairs(prev => [
      ...prev,
      {
        question: questionText,
        answer: typeof assistantMessage.content === 'string' ? assistantMessage.content : '',
        followUps
      }
    ])

    return abortController.abort()
  }

  const clearChat = async () => {
    setClearingChat(true)
    if (appStateContext?.state.currentChat?.id && appStateContext?.state.isCosmosDBAvailable.cosmosDB) {
      let response = await historyClear(appStateContext?.state.currentChat.id)
      if (!response.ok) {
        setErrorMsg({
          title: 'Error clearing current chat',
          subtitle: 'Please try again. If the problem persists, please contact the site administrator.'
        })
        toggleErrorDialog()
      } else {
        appStateContext?.dispatch({
          type: 'DELETE_CURRENT_CHAT_MESSAGES',
          payload: appStateContext?.state.currentChat.id
        })
        appStateContext?.dispatch({ type: 'UPDATE_CHAT_HISTORY', payload: appStateContext?.state.currentChat })
        setIsCitationPanelOpen(false)
        setIsIntentsPanelOpen(false)
        setMessages([])
      }
    }
    setClearingChat(false)
  }

  const tryGetRaiPrettyError = (errorMessage: string) => {
    try {
      // Using a regex to extract the JSON part that contains "innererror"
      const match = errorMessage.match(/'innererror': ({.*})\}\}/)
      if (match) {
        // Replacing single quotes with double quotes and converting Python-like booleans to JSON booleans
        const fixedJson = match[1]
          .replace(/'/g, '"')
          .replace(/\bTrue\b/g, 'true')
          .replace(/\bFalse\b/g, 'false')
        const innerErrorJson = JSON.parse(fixedJson)
        let reason = ''
        // Check if jailbreak content filter is the reason of the error
        const jailbreak = innerErrorJson.content_filter_result.jailbreak
        if (jailbreak.filtered === true) {
          reason = 'Jailbreak'
        }

        // Returning the prettified error message
        if (reason !== '') {
          return 'The prompt was filtered due to triggering Azure OpenAI\'s content filtering system.\n' +
            'Reason: This prompt contains content flagged as ' + reason +
            '\n\nPlease modify your prompt and retry. Learn more: https://go.microsoft.com/fwlink/?linkid=2198766';
        }
      }
    } catch (e) {
      console.error('Failed to parse the error:', e)
    }
    return errorMessage
  }

  const parseErrorMessage = (errorMessage: string) => {
    let errorCodeMessage = errorMessage.substring(0, errorMessage.indexOf('-') + 1)
    const innerErrorCue = "{\\'error\\': {\\'message\\': "
    if (errorMessage.includes(innerErrorCue)) {
      try {
        let innerErrorString = errorMessage.substring(errorMessage.indexOf(innerErrorCue))
        if (innerErrorString.endsWith("'}}")) {
          innerErrorString = innerErrorString.substring(0, innerErrorString.length - 3)
        }
        innerErrorString = innerErrorString.replaceAll("\\'", "'")
        let newErrorMessage = errorCodeMessage + ' ' + innerErrorString
        errorMessage = newErrorMessage
      } catch (e) {
        console.error('Error parsing inner error message: ', e)
      }
    }

    return tryGetRaiPrettyError(errorMessage)
  }

  const newChat = () => {
    setProcessMessages(messageStatus.Processing)
    setMessages([])
    setIsCitationPanelOpen(false)
    setIsIntentsPanelOpen(false)
    appStateContext?.dispatch({ type: 'UPDATE_CURRENT_CHAT', payload: null })
    setProcessMessages(messageStatus.Done)
  }

  const stopGenerating = () => {
    abortFuncs.current.forEach(a => a.abort())
    setShowLoadingMessage(false)
    setIsLoading(false)
  }

  useEffect(() => {
    if (appStateContext?.state.currentChat) {
      setMessages(appStateContext.state.currentChat.messages)
    } else {
      setMessages([])
    }
  }, [appStateContext?.state.currentChat])

  useLayoutEffect(() => {
    const saveToDB = async (messages: ChatMessage[], id: string) => {
      const response = await historyUpdate(messages, id)
      return response
    }

    if (appStateContext && appStateContext.state.currentChat && processMessages === messageStatus.Done) {
      if (appStateContext.state.isCosmosDBAvailable.cosmosDB) {
        if (!appStateContext?.state.currentChat?.messages) {
          console.error('Failure fetching current chat state.')
          return
        }
        const noContentError = appStateContext.state.currentChat.messages.find(m => m.role === ERROR)

        if (!noContentError) {
          saveToDB(appStateContext.state.currentChat.messages, appStateContext.state.currentChat.id)
            .then(res => {
              if (!res.ok) {
                let errorMessage =
                  "An error occurred. Answers can't be saved at this time. If the problem persists, please contact the site administrator."
                let errorChatMsg: ChatMessage = {
                  id: uuid(),
                  role: ERROR,
                  content: errorMessage,
                  date: new Date().toISOString()
                }
                if (!appStateContext?.state.currentChat?.messages) {
                  let err: Error = {
                    ...new Error(),
                    message: 'Failure fetching current chat state.'
                  }
                  throw err
                }
                setMessages([...appStateContext?.state.currentChat?.messages, errorChatMsg])
              }
              return res as Response
            })
            .catch(err => {
              console.error('Error: ', err)
              let errRes: Response = {
                ...new Response(),
                ok: false,
                status: 500
              }
              return errRes
            })
        }
      } else {
      }
      appStateContext?.dispatch({ type: 'UPDATE_CHAT_HISTORY', payload: appStateContext.state.currentChat })
      setMessages(appStateContext.state.currentChat.messages)
      setProcessMessages(messageStatus.NotRunning)
    }
  }, [processMessages])

  useEffect(() => {
    if (AUTH_ENABLED !== undefined) getUserInfoList()
  }, [AUTH_ENABLED])

  useLayoutEffect(() => {
    chatMessageStreamEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [showLoadingMessage, processMessages])

  const onShowCitation = (citation: Citation) => {
    console.log('==== CITATION CLICK HANDLING START ====');
    console.log('Citation clicked - full citation object:', JSON.stringify(citation, null, 2));

    // If URL exists, open it directly
    if (citation.url) {
      console.log(`Citation has URL: ${citation.url}, opening directly`);
      window.open(citation.url, '_blank');
      console.log('==== CITATION CLICK HANDLING END ====');
      return;
    }

    // Check if citation might have metadata_storage_path but no filepath
    if (!citation.filepath && citation.metadata) {
      console.log('Citation has no filepath but has metadata:', citation.metadata);
      try {
        const metadata = JSON.parse(citation.metadata);
        console.log('Parsed metadata:', JSON.stringify(metadata, null, 2));
        if (metadata.metadata_storage_path) {
          console.log(`Found metadata_storage_path: ${metadata.metadata_storage_path}`);
          // Extract just the filename from the metadata_storage_path
          const pathParts = metadata.metadata_storage_path.split('/');
          const filename = pathParts[pathParts.length - 1];
          citation.filepath = filename;
          console.log(`Extracted filename from metadata_storage_path: ${filename}`);
        } else {
          console.log('No metadata_storage_path found in metadata');
          // Check for other potential filename fields
          if (metadata.filepath) {
            console.log(`Found filepath in metadata: ${metadata.filepath}`);
            citation.filepath = metadata.filepath;
          } else if (metadata.filename) {
            console.log(`Found filename in metadata: ${metadata.filename}`);
            citation.filepath = metadata.filename;
          }
        }
      } catch (e) {
        console.error("Failed to parse metadata JSON:", e);
      }
    }

    if (citation.filepath) {
      console.log(`Working with filepath: ${citation.filepath}`);
      // Extract just the filename from the filepath, regardless of path format
      const filename = citation.filepath.split(/[\/\\]/).pop();
      console.log('Extracted filename:', filename);

      if (filename) {
        // Try to decode URI encoded filename if needed
        let decodedFilename = filename;
        try {
          if (filename.includes('%')) {
            decodedFilename = decodeURIComponent(filename);
            console.log(`Decoded filename: ${filename} -> ${decodedFilename}`);
          }
        } catch (e) {
          console.error("Error decoding filename:", e);
        }

        // Get the base URL from current window location
        const baseUrl = window.location.origin;
        console.log(`Base URL: ${baseUrl}`);

        // Only check site_pdfs directory
        console.log(`Checking if file exists in site_pdfs directory: /site_pdfs/${decodedFilename}`);
        fetch(`/site_pdfs/${decodedFilename}`, { method: 'HEAD' })
          .then(response => {
            console.log(`Site_pdfs directory check result: ${response.status} ${response.ok ? 'OK' : 'Not Found'}`);
            if (response.ok) {
              // File exists in site_pdfs
              const pdfUrl = `${baseUrl}/site_pdfs/${decodedFilename}`;
              const pageParam = citation.page ? `#page=${citation.page}` : '';
              const fullUrl = `${pdfUrl}${pageParam}`;
              console.log(`Opening PDF from site_pdfs directory: ${fullUrl}`);
              window.open(fullUrl, '_blank');
              console.log('==== CITATION CLICK HANDLING END ====');
            } else {
              console.error(`PDF not found in site_pdfs directory: ${decodedFilename}`);
              alert(`Source document '${decodedFilename}' not available. Please check if the file exists in the site_pdfs directory.`);
              console.log('==== CITATION CLICK HANDLING END ====');
            }
          })
          .catch(error => {
            console.error('Error checking site_pdfs directory:', error);
            alert('Error accessing the PDF document.');
            console.log('==== CITATION CLICK HANDLING END ====');
          });
      } else {
        console.error('Could not extract filename from filepath:', citation.filepath);
        alert(`Unable to extract filename from: ${citation.filepath}`);
        console.log('==== CITATION CLICK HANDLING END ====');
      }
    } else {
      console.error('No URL or direct filepath in citation:', citation);

      // Fallback: Try to use the title as the filename if available
      if (citation.title) {
        console.log('Attempting to use title as filename:', citation.title);
        const sanitizedTitle = citation.title.replace(/[^a-zA-Z0-9]/g, '_').toLowerCase() + '.pdf';
        console.log(`Sanitized title: ${sanitizedTitle}`);

        const baseUrl = window.location.origin;
        console.log(`Checking if title-based file exists in site_pdfs directory: /site_pdfs/${sanitizedTitle}`);
        fetch(`/site_pdfs/${sanitizedTitle}`, { method: 'HEAD' })
          .then(response => {
            console.log(`Title-based file check result: ${response.status} ${response.ok ? 'OK' : 'Not Found'}`);
            if (response.ok) {
              const pdfUrl = `${baseUrl}/site_pdfs/${sanitizedTitle}`;
              const pageParam = citation.page ? `#page=${citation.page}` : '';
              const fullUrl = `${pdfUrl}${pageParam}`;
              console.log(`Opening PDF from site_pdfs directory (using title): ${fullUrl}`);
              window.open(fullUrl, '_blank');
              console.log('==== CITATION CLICK HANDLING END ====');
            } else {
              console.error(`Title-based PDF not found in site_pdfs directory: ${sanitizedTitle}`);
              alert(`Source document '${sanitizedTitle}' (derived from title) not available. Please check if the file exists in the site_pdfs directory.`);
              console.log('==== CITATION CLICK HANDLING END ====');
            }
          })
          .catch(error => {
            console.error('Error checking site_pdfs directory for title-based fallback:', error);
            alert('Error accessing the PDF document using title-based fallback.');
            console.log('==== CITATION CLICK HANDLING END ====');
          });
      } else {
        alert('Source document not available for this citation. No URL, filepath, or title provided to locate the document.');
        console.log('==== CITATION CLICK HANDLING END ====');
      }
    }
  }

  const onShowExecResult = (answerId: string) => {
    setIsIntentsPanelOpen(true)
  }

  const parseCitationFromMessage = (message: ChatMessage) => {
    if (message?.role && message?.role === 'tool' && typeof message?.content === "string") {
      try {
        const toolMessage = JSON.parse(message.content) as ToolMessageContent
        return toolMessage.citations
      } catch {
        return []
      }
    }
    return []
  }

  const parsePlotFromMessage = (message: ChatMessage) => {
    if (message?.role && message?.role === "tool" && typeof message?.content === "string") {
      try {
        const execResults = JSON.parse(message.content) as AzureSqlServerExecResults;
        const codeExecResult = execResults.all_exec_results.at(-1)?.code_exec_result;

        if (codeExecResult === undefined) {
          return null;
        }
        return codeExecResult.toString();
      }
      catch {
        return null;
      }
      // const execResults = JSON.parse(message.content) as AzureSqlServerExecResults;
      // return execResults.all_exec_results.at(-1)?.code_exec_result;
    }
    return null;
  }

  const disabledButton = () => {
    return (
      isLoading ||
      (messages && messages.length === 0) ||
      clearingChat ||
      appStateContext?.state.chatHistoryLoadingState === ChatHistoryLoadingState.Loading
    )
  }

  // Improved function to properly highlight text within HTML content
  const highlightTextInContent = (content: string, textToHighlight: string) => {
    if (!textToHighlight || !content) return content;
    
    try {
      // Normalize text by removing extra whitespace
      const normalizedContent = content.replace(/\s+/g, ' ').trim();
      const normalizedHighlight = textToHighlight.replace(/\s+/g, ' ').trim();
      
      // Don't try to highlight if the highlight text is too short
      if (normalizedHighlight.length < 5) {
        // Just return the first sentence instead
        const sentences = normalizedContent.match(/[^.!?]+[.!?]+/g) || [];
        if (sentences.length > 0 && sentences[0]) {
          return normalizedContent.replace(
            sentences[0],
            `<span class="${styles.highlightCitation}">${sentences[0]}</span>`
          );
        }
        return content;
      }
      
      // Escape special regex characters in the text to highlight
      const escapedText = normalizedHighlight.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      
      // Create a case-insensitive regex to match the text
      const regex = new RegExp(`(${escapedText})`, 'gi');
      
      // Check if the highlight text is found in the content
      if (!normalizedContent.match(regex)) {
        // If exact match not found, try to find partial match
        // First split into words and look for notable phrases
        const words = normalizedHighlight.split(/\s+/).filter(w => w.length > 5);
        
        // Try to find phrases of 3-4 words that appear in the content
        if (words.length >= 4) {
          for (let i = 0; i < words.length - 3; i++) {
            const phrase = words.slice(i, i + 4).join(' ');
            const escapedPhrase = phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const phraseRegex = new RegExp(`(${escapedPhrase})`, 'gi');
            
            if (normalizedContent.match(phraseRegex)) {
              return normalizedContent.replace(
                phraseRegex,
                match => `<span class="${styles.highlightCitation}">${match}</span>`
              );
            }
          }
        }
        
        // Try sentences if no phrases matched
        const sentences = normalizedContent.match(/[^.!?]+[.!?]+/g) || [];
        if (sentences.length > 0 && sentences[0]) {
          const firstSentence = sentences[0];
          return normalizedContent.replace(
            firstSentence,
            `<span class="${styles.highlightCitation}">${firstSentence}</span>`
          );
        }
        
        // Fall back to first 100 characters if nothing else worked
        const firstPart = normalizedContent.substring(0, Math.min(100, normalizedContent.length));
        const escapedFirstPart = firstPart.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const firstPartRegex = new RegExp(`(${escapedFirstPart})`, 'g');
        
        return normalizedContent.replace(
          firstPartRegex,
          match => `<span class="${styles.highlightCitation}">${match}</span>`
        );
      }
      
      // If we found a match, highlight it
      return normalizedContent.replace(
        regex,
        match => `<span class="${styles.highlightCitation}">${match}</span>`
      );
    } catch (error) {
      console.error('Error highlighting text:', error);
      
      // Fallback to highlighting first few sentences
      try {
        const sentences = content.match(/[^.!?]+[.!?]+/g) || [];
        if (sentences.length > 0 && sentences[0]) {
          return content.replace(
            sentences[0],
            `<span class="${styles.highlightCitation}">${sentences[0]}</span>`
          );
        }
        return content;
      } catch {
        return content;
      }
    }
  }

  // Simulate fetching follow-ups and answer from Cosmos DB
  const fetchFollowUps = useCallback(async (question: string) => {
    const res = await fetch(`/api/similar-questions?query=${encodeURIComponent(question)}`);
    if (!res.ok) return [];
    return await res.json(); // Should be [{ id, text }, ...]
  }, []);

  const fetchAnswerFromCosmos = useCallback(async (questionId: string) => {
    const res = await fetch(`/api/answer/${questionId}`);
    if (!res.ok) return 'No answer found.';
    const data = await res.json();
    return data.answer;
  }, []);

  // When a new user question is asked
  const handleUserQuestion = async (question: string) => {
    // No need to make API call here anymore since it's called from QuestionInput
    
    // Fetch follow-up questions after receiving the answer
    const followUps = await fetchFollowUps(question)
    
    // Only update qaPairs with the latest answer from messages
    if (messages.length > 0) {
      const latestAnswer = messages[messages.length - 1]
      const answerContent = typeof latestAnswer.content === 'string' ? latestAnswer.content : ''
      
      setQaPairs(prev => [...prev, { 
        question, 
        answer: answerContent, 
        followUps 
      }])
    }
  }

  // When a follow-up is clicked
  const handleFollowUpClick = async (followUp: { id: string, text: string }) => {
    // Send the follow-up text as a new question
    if (appStateContext?.state.isCosmosDBAvailable?.cosmosDB) {
      await makeApiRequestWithCosmosDB(followUp.text)
    } else {
      await makeApiRequestWithoutCosmosDB(followUp.text)
    }
    
    // Get more follow-ups for this question
    const newFollowUps = await fetchFollowUps(followUp.text)
    
    // Update qaPairs with the latest Q&A
    if (messages.length > 0) {
      const latestAnswer = messages[messages.length - 1]
      const answerContent = typeof latestAnswer.content === 'string' ? latestAnswer.content : ''
      
      setQaPairs(prev => [...prev, { 
        question: followUp.text, 
        answer: answerContent, 
        followUps: newFollowUps 
      }])
    }
  }

  return (
    <div className={styles.container} role="main">
      {showAuthMessage ? (
        <Stack className={styles.chatEmptyState}>
          <ShieldLockRegular
            className={styles.chatIcon}
            style={{ color: 'darkorange', height: '200px', width: '200px' }}
          />
          <h1 className={styles.chatEmptyStateTitle}>Authentication Not Configured</h1>
          <h2 className={styles.chatEmptyStateSubtitle}>
            This app does not have authentication configured. Please add an identity provider by finding your app in the{' '}
            <a href="https://portal.azure.com/" target="_blank">
              Azure Portal
            </a>
            and following{' '}
            <a
              href="https://learn.microsoft.com/en-us/azure/app-service/scenario-secure-app-authentication-app-service#3-configure-authentication-and-authorization"
              target="_blank">
              these instructions
            </a>
            .
          </h2>
          <h2 className={styles.chatEmptyStateSubtitle} style={{ fontSize: '20px' }}>
            <strong>Authentication configuration takes a few minutes to apply. </strong>
          </h2>
          <h2 className={styles.chatEmptyStateSubtitle} style={{ fontSize: '20px' }}>
            <strong>If you deployed in the last 10 minutes, please wait and reload the page after 10 minutes.</strong>
          </h2>
        </Stack>
      ) : (
        <Stack horizontal className={styles.chatRoot}>
          <div className={styles.chatContainer}>
            {!messages || messages.length < 1 ? (
              <Stack className={styles.chatEmptyState}>
                <img src={logo} className={styles.chatIcon} aria-hidden="true" />
                <h1 className={styles.chatEmptyStateTitle}>{ui?.chat_title}</h1>
                <h2 className={styles.chatEmptyStateSubtitle}>{ui?.chat_description}</h2>
              </Stack>
            ) : (
              <div className={styles.chatMessageStream} style={{ marginBottom: isLoading ? '40px' : '0px' }} role="log">
                {messages.map((answer, index) => (
                  <>
                    {answer.role === 'user' ? (
                      <div className={styles.chatMessageUser} tabIndex={0}>
                        <div className={styles.chatMessageUserMessage}>
                          {typeof answer.content === "string" && answer.content ? answer.content : Array.isArray(answer.content) ? <>{answer.content[0].text} <img className={styles.uploadedImageChat} src={answer.content[1].image_url.url} alt="Uploaded Preview" /></> : null}
                        </div>
                      </div>
                    ) : answer.role === 'assistant' ? (
                      <div className={styles.chatMessageGpt}>
                        {typeof answer.content === "string" && <div className={styles.chatMessageGptContent}>
                          <Answer
                            answer={{
                              answer: answer.content,
                              citations: parseCitationFromMessage(messages[index - 1]),
                              generated_chart: parsePlotFromMessage(messages[index - 1]),
                              message_id: answer.id,
                              feedback: answer.feedback,
                              exec_results: execResults
                            }}
                            onCitationClicked={c => onShowCitation(c)}
                            onExectResultClicked={() => onShowExecResult(answerId)}
                            followUpQuestions={qaPairs.length > 0 && index === messages.length - 1 ? qaPairs[qaPairs.length - 1].followUps : []}
                            onFollowUpClick={handleFollowUpClick}
                          />
                        </div>}
                      </div>
                    ) : answer.role === ERROR ? (
                      <div className={styles.chatMessageError}>
                        <Stack horizontal className={styles.chatMessageErrorContent}>
                          <ErrorCircleRegular className={styles.errorIcon} style={{ color: 'rgba(182, 52, 67, 1)' }} />
                          <span>Error</span>
                        </Stack>
                        <span className={styles.chatMessageErrorContent}>{typeof answer.content === "string" && answer.content}</span>
                      </div>
                    ) : null}
                  </>
                ))}
                {showLoadingMessage && (
                  <>
                    <div className={styles.chatMessageGpt}>
                      <div className={styles.chatMessageGptContent}>
                        <Answer
                          answer={{
                            answer: "Generating answer...",
                            citations: [],
                            generated_chart: null
                          }}
                          onCitationClicked={() => null}
                          onExectResultClicked={() => null}
                        />
                      </div>
                    </div>
                  </>
                )}
                <div ref={chatMessageStreamEnd} />
              </div>
            )}

            <Stack horizontal className={styles.chatInput}>
              {isLoading && messages.length > 0 && (
                <Stack
                  horizontal
                  className={styles.stopGeneratingContainer}
                  role="button"
                  aria-label="Stop generating"
                  tabIndex={0}
                  onClick={stopGenerating}
                  onKeyDown={e => (e.key === 'Enter' || e.key === ' ' ? stopGenerating() : null)}>
                  <SquareRegular className={styles.stopGeneratingIcon} aria-hidden="true" />
                  <span className={styles.stopGeneratingText} aria-hidden="true">
                    Stop generating
                  </span>
                </Stack>
              )}
              <Stack>
                {appStateContext?.state.isCosmosDBAvailable?.status !== CosmosDBStatus.NotConfigured && (
                  <CommandBarButton
                    role="button"
                    styles={{
                      icon: {
                        color: '#FFFFFF'
                      },
                      iconDisabled: {
                        color: '#BDBDBD !important'
                      },
                      root: {
                        color: '#FFFFFF',
                        background:
                          'radial-gradient(109.81% 107.82% at 100.1% 90.19%, #0F6CBD 33.63%, #2D87C3 70.31%, #8DDDD8 100%)'
                      },
                      rootDisabled: {
                        background: '#F0F0F0'
                      }
                    }}
                    className={styles.newChatIcon}
                    iconProps={{ iconName: 'Add' }}
                    onClick={newChat}
                    disabled={disabledButton()}
                    aria-label="start a new chat button"
                  />
                )}
                <CommandBarButton
                  role="button"
                  styles={{
                    icon: {
                      color: '#FFFFFF'
                    },
                    iconDisabled: {
                      color: '#BDBDBD !important'
                    },
                    root: {
                      color: '#FFFFFF',
                      background:
                        'radial-gradient(109.81% 107.82% at 100.1% 90.19%, #0F6CBD 33.63%, #2D87C3 70.31%, #8DDDD8 100%)'
                    },
                    rootDisabled: {
                      background: '#F0F0F0'
                    }
                  }}
                  className={
                    appStateContext?.state.isCosmosDBAvailable?.status !== CosmosDBStatus.NotConfigured
                      ? styles.clearChatBroom
                      : styles.clearChatBroomNoCosmos
                  }
                  iconProps={{ iconName: 'Broom' }}
                  onClick={
                    appStateContext?.state.isCosmosDBAvailable?.status !== CosmosDBStatus.NotConfigured
                      ? clearChat
                      : newChat
                  }
                                    disabled={disabledButton()}
                  aria-label="clear chat button"
                />
              <Dialog
                hidden={hideErrorDialog}
                onDismiss={handleErrorDialogClose}
                dialogContentProps={errorDialogContentProps}
                modalProps={modalProps}></Dialog>
            </Stack>
            </Stack>
            <QuestionInput
              clearOnSend
              placeholder="Ask your question..."
              disabled={isLoading}
              onSend={(question, id) => {
                // Regular API call
                appStateContext?.state.isCosmosDBAvailable?.cosmosDB
                  ? makeApiRequestWithCosmosDB(question, id)
                  : makeApiRequestWithoutCosmosDB(question, id)
                
                // Also fetch follow-up questions for string queries
                if (typeof question === 'string') {
                  // Use setTimeout to ensure this runs after the API call is initiated
                  setTimeout(() => handleUserQuestion(question), 100);
                }
              }}
              conversationId={
                appStateContext?.state.currentChat?.id ? appStateContext?.state.currentChat?.id : undefined
              }
            />
          </div>
          {messages && messages.length > 0 && isIntentsPanelOpen && (
            <Stack.Item className={styles.citationPanel} tabIndex={0} role="tabpanel" aria-label="Intents Panel">
              <Stack
                aria-label="Intents Panel Header Container"
                horizontal
                className={styles.citationPanelHeaderContainer}
                horizontalAlign="space-between"
                verticalAlign="center">
                <span aria-label="Intents" className={styles.citationPanelHeader}>
                  Intents
                </span>
                <IconButton
                  iconProps={{ iconName: 'Cancel' }}
                  aria-label="Close intents panel"
                  onClick={() => setIsIntentsPanelOpen(false)}
                />
              </Stack>
              <Stack horizontalAlign="space-between">
                {appStateContext?.state?.answerExecResult[answerId]?.map((execResult: ExecResults, index) => (
                  <Stack className={styles.exectResultList} verticalAlign="space-between">
                    <><span>Intent:</span> <p>{execResult.intent}</p></>
                    {execResult.search_query && <><span>Search Query:</span>
                      <SyntaxHighlighter
                        style={nord}
                        wrapLines={true}
                        lineProps={{ style: { wordBreak: 'break-all', whiteSpace: 'pre-wrap' } }}
                        language="sql"
                        PreTag="p">
                        {execResult.search_query}
                      </SyntaxHighlighter></>}
                    {execResult.search_result && <><span>Search Result:</span> <p>{execResult.search_result}</p></>}
                    {execResult.code_generated && <><span>Code Generated:</span>
                      <SyntaxHighlighter
                        style={nord}
                        wrapLines={true}
                        lineProps={{ style: { wordBreak: 'break-all', whiteSpace: 'pre-wrap' } }}
                        language="python"
                        PreTag="p">
                        {execResult.code_generated}
                      </SyntaxHighlighter>
                    </>}
                  </Stack>
                ))}
              </Stack>
            </Stack.Item>
          )}
          {appStateContext?.state.isChatHistoryOpen &&
            appStateContext?.state.isCosmosDBAvailable?.status !== CosmosDBStatus.NotConfigured && <ChatHistoryPanel />}
        </Stack>
      )}
    </div>
  )
}

export default Chat
