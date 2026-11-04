# Data Flows and Regions

A SMART stack runs in your AWS account, in the Region you deploy it to (`region` in `socialagent.<env>.yaml`). The stack's resources are created there, among them AgentCore Memory, the S3 buckets, the optional DynamoDB state table and the CloudWatch logs. Some of the calls a stack makes process content somewhere else: in other AWS Regions, or outside AWS. This page lists them, with what decides each one, so you can check a deployment against your data-residency requirements. For how each AWS service involved behaves, the AWS documentation linked here is the authority.

| Flow | What it carries | Where it can be processed | What decides it |
|------|-----------------|---------------------------|-----------------|
| [Agent model calls](#amazon-bedrock-cross-region-inference) | Messages, voice-note transcripts, conversation history, tool results with user state; inbound images and PDFs in `personal-assistant` | The destination Regions of the Bedrock inference profile: one geography, or any supported commercial Region | The model ID's inference profile and the Region the stack calls it from |
| [Image generation](#image-generation) | The image prompt the agent writes | The Region in `IMAGE_MODEL_REGION`, us-west-2 by default | Whether the `generate-image` recipe is wired in, and `IMAGE_MODEL_REGION` in `agent.environment` |
| [Voice notes and voice replies](#amazon-transcribe-and-amazon-polly) | Inbound voice-note audio (Transcribe); the text of a voice reply (Polly) | The stack's Region; content used for service improvement may be stored in another Region | An AWS Organizations AI services opt-out policy |
| [WhatsApp delivery](#whatsapp-delivery-through-meta) | Every message in and out, media and templates | Meta, outside AWS | Inherent to the channel |
| [Admin console](#admin-console) (optional) | The conversations and per-user state an operator opens | The console's Region (`aws_region` of the console's Terraform, us-east-1 by default) | Whether you deploy the console, and where |

## Amazon Bedrock: cross-Region inference

By default, the `personal-assistant` and `appointment-scheduler` blueprints call their model through an Amazon Bedrock [cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html) profile. [Configuration](configuration.md#agent) says where a project chooses its model. The `minimal` blueprint calls no model, and a custom agent calls whatever model ID you give it.

The prefix of the model ID names the kind of profile:

- A geographic profile, such as `us.`, `eu.`, `apac.`, `au.` or `jp.`, keeps inference in the Regions of that geography ([Geographic cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html)).
- The global profile, `global.`, may route a request to any supported AWS commercial Region ([Global cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html)).
- A model ID without a profile prefix names no profile, and runs in the Region you call it from, for a model that offers in-Region inference there.

The prefix alone does not say where a request can go: a profile's destination Regions can differ by source Region, the Region the stack calls it from. If you have data-residency requirements, check the destination Regions from the stack's Region, either with `aws bedrock get-inference-profile --region <stack region> --inference-profile-identifier <model id> --query 'models[].modelArn'`, which returns one model ARN per destination Region, or on the model's page in [models at a glance](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html). [Supported Regions and models for inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html) explains both ways. The stack's IAM policies allow invoking a foundation model in any Region, which cross-Region inference needs, so they do not narrow where a profile routes. An organization that must enforce the choice can do it with service control policies, as those pages describe.

Every model call the agent makes goes through the profile, and carries:

- the end user's messages, button and list replies, shared locations and the transcripts of voice notes included
- the conversation history that AgentCore Memory restores for the session
- the system prompt and the results of the agent's tool calls, among them the per-user state the tools read, such as what the personal assistant was asked to remember
- in a scheduled agent turn, the task prompt stored with the schedule

In `personal-assistant`, the webhook Lambda also sends inbound images and PDF documents to Bedrock through a cross-Region inference profile, by default with the agent's model, to describe or summarize them for the agent.

Bedrock may store prompts and outputs for [abuse detection](https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html). AWS documents that, to the extent it stores them, a cross-Region inference request has its prompts and outputs stored in the destination Region, which can be outside the stack's Region, and that a profile can route to opt-in Regions that your account has not enabled ([Geographic cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html), [Supported Regions and models for inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)).

## Image generation

The `generate-image` recipe, which `personal-assistant` ships wired in, calls its image model through Bedrock in one Region, whatever the stack's Region: the Region in the `IMAGE_MODEL_REGION` environment variable. With the variable unset or empty, the default keeps the request in us-west-2, where the model is offered. This is a direct call to that Region, not cross-Region inference, and the agent's model ID does not change it. It sends the image prompt the agent writes from the conversation; the image that comes back is stored in the stack's media bucket and sent to WhatsApp.

To choose the Region, set `IMAGE_MODEL_REGION` under `agent.environment` in `socialagent.<env>.yaml` and deploy:

```yaml
agent:
  environment:
    IMAGE_MODEL_REGION: <region>
```

The recipe's image model must be available in the Region you choose: the recipe calls it by its model ID, `stability.sd3-5-large-v1:0`, not through an inference profile. Check the model's page in [models at a glance](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html). In a Region without the model, the call fails and the agent gets an error instead of an image. The stack's IAM policies allow invoking a foundation model in any Region, so a different Region needs no IAM change. To keep the prompt in the stack's Region, set the stack's Region, if the model is available there.

The stack's audit logs record an image call only when `IMAGE_MODEL_REGION` is the stack's Region. The CloudTrail trail and the Bedrock model invocation logging that `enable_security_logging` turns on ([Configuration](configuration.md#terraform)) cover the stack's Region alone, so a call to any other Region is not in them, and that includes the us-west-2 default from a stack deployed anywhere else.

The variable is read by the project's copy of the recipe, `src/tools/generate_image.py`. `socialagent recipe add` never overwrites an existing file, so a copy taken before the recipe read `IMAGE_MODEL_REGION` ignores it and still calls us-west-2: check that the project's copy reads the variable.

To send no image prompts to Bedrock at all, leave the recipe out of a project, or, in a `personal-assistant` project, remove `generate_image_tool` from `socialagent_tools` in `src/tools/__init__.py` and delete `src/skills/generate_image.md`.

## Amazon Transcribe and Amazon Polly

- Every stack sends inbound voice notes to Amazon Transcribe in the stack's Region, so the agent receives a transcript ([Media](media.md)).
- The `voice-reply` recipe, which `personal-assistant` ships wired in, sends the text of each voice reply to Amazon Polly in the stack's Region.

By default, AWS AI services, Transcribe and Polly among them, may store and use the content they process to improve those services, and AWS may store that content in a Region other than the one where you use the service. A customer opts out with an AWS Organizations [AI services opt-out policy](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_ai-opt-out.html); see also [Opting out of using your data for service improvement](https://docs.aws.amazon.com/transcribe/latest/dg/opt-out.html) for Transcribe and the [Amazon Polly FAQs](https://aws.amazon.com/polly/faqs/). SMART sets no opt-out policy: it is an AWS Organizations policy, attached to the organization's root, an organizational unit or an account, so it is the customer's choice.

## WhatsApp delivery through Meta

WhatsApp messages leave AWS. [AWS End User Messaging Social](https://docs.aws.amazon.com/social-messaging/latest/userguide/what-is-service.html) hands every outbound message and media file to Meta's WhatsApp Business Platform for delivery and receives the inbound ones from it, and the templates that `socialagent templates sync` registers go to Meta for review. Meta handles that content under the WhatsApp Business terms that apply to the WhatsApp Business Account ([Sending messages through WhatsApp](https://docs.aws.amazon.com/social-messaging/latest/userguide/whatsapp-send-message.html)).

## Admin console

The optional [admin console](admin-console.md) runs in its own Region, `aws_region` in the console's Terraform (`socialagent-admin-console/infra-terraform`), us-east-1 by default, which need not be the stack's. Its Amazon Cognito sign-in, its Amazon API Gateway API and its backend Lambda function run there. When an operator opens a conversation or a user's state, the backend reads it from the stack's Region and returns it to the operator's browser through that API, so the content is processed in the console's Region. Amazon CloudFront serves only the console's static pages, not that content.
