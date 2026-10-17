from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import skipUnlessDBFeature, TransactionTestCase
from rest_framework.test import APITestCase, APIClient

from tasks.models import CustomerRequest, Project, Item


class CustomerPortalAPITests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            org_id=1,
            name='Pulse',
            key='PLS',
            access_level='limited',
            settings={
                'customer_portal_enabled': True,
                'customer_portal_public_ref': 'pls-portal-7d9b2a6f',
                'customer_request_assignment_group': 'product',
            },
        )
        self.portal_ref = self.project.settings['customer_portal_public_ref']

    def test_public_submission_list_vote_and_comments_hide_emails(self):
        create = self.client.post(
            f'/api/customer-portal/{self.portal_ref}/requests/',
            {
                'title': 'Let customers vote on roadmap ideas',
                'description': 'Customers need a simple way to submit and vote on feature requests.',
                'category': 'roadmap',
                'requester_name': 'Ada',
                'requester_email': 'ada@example.com',
            },
            format='json',
        )
        self.assertEqual(create.status_code, 201)
        request_id = create.data['id']
        self.assertNotIn('requester_email', create.data)

        first_vote = self.client.post(
            f'/api/customer-portal/{self.portal_ref}/requests/{request_id}/vote/',
            {'voter_email': 'fan@example.com'},
            format='json',
        )
        duplicate_vote = self.client.post(
            f'/api/customer-portal/{self.portal_ref}/requests/{request_id}/vote/',
            {'voter_email': 'fan@example.com'},
            format='json',
        )
        self.assertEqual(first_vote.status_code, 200)
        self.assertEqual(duplicate_vote.status_code, 200)
        self.assertEqual(first_vote.data['upvote_count'], 1)
        self.assertEqual(duplicate_vote.data['upvote_count'], 1)
        self.assertFalse(duplicate_vote.data['created'])

        comment = self.client.post(
            f'/api/customer-portal/{self.portal_ref}/requests/{request_id}/comments/',
            {'author_name': 'Grace', 'author_email': 'grace@example.com', 'body': 'This would help our beta users.'},
            format='json',
        )
        self.assertEqual(comment.status_code, 201)
        self.assertNotIn('author_email', comment.data)

        listing = self.client.get(f'/api/customer-portal/{self.portal_ref}/requests/')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data['project']['key'], self.project.key)
        self.assertNotIn('id', listing.data['project'])
        self.assertEqual(listing.data['results'][0]['upvote_count'], 1)
        self.assertNotIn('requester_email', listing.data['results'][0])

    def test_public_portal_requires_explicit_non_enumerable_opt_in_ref(self):
        disabled = Project.objects.create(
            org_id=1,
            name='Disabled portal',
            key='DIS',
            access_level='open',
            settings={'customer_portal_public_ref': 'disabled-secret'},
        )
        private = Project.objects.create(
            org_id=1,
            name='Private portal',
            key='PRV',
            access_level='private',
            settings={'customer_portal_enabled': False, 'customer_portal_public_ref': 'private-secret'},
        )

        for ref in [str(self.project.id), self.project.key, disabled.key, 'disabled-secret', private.key, 'private-secret']:
            response = self.client.get(f'/api/customer-portal/{ref}/requests/')
            self.assertEqual(response.status_code, 404, ref)

    def test_approve_customer_request_creates_internal_pulse_item_once(self):
        customer_request = CustomerRequest.objects.create(
            project=self.project,
            title='Expose public roadmap status',
            description='Customers should see planned, in progress, and shipped status.',
            category='roadmap',
            upvote_count=7,
        )
        user = get_user_model().objects.create_user(
            username='staff',
            email='staff@example.com',
            password='secret',
            is_staff=True,
        )
        self.client.force_authenticate(user=user)

        first = self.client.post(
            f'/api/customer-portal/requests/{customer_request.id}/approve/',
            {'priority': 'normal', 'workflow': 'autonomous-dev', 'assignment_group': 'engineering'},
            format='json',
        )
        second = self.client.post(
            f'/api/customer-portal/requests/{customer_request.id}/approve/',
            {'priority': 'normal'},
            format='json',
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.data['created'])
        self.assertFalse(second.data['created'])
        self.assertEqual(Item.objects.filter(source='customer_request_portal', source_id=str(customer_request.id)).count(), 1)
        item = Item.objects.get(source='customer_request_portal', source_id=str(customer_request.id))
        self.assertEqual(item.project, self.project)
        self.assertEqual(item.assignment_group, 'product')
        self.assertEqual(item.workflow_name, 'feature-request-rfc')
        self.assertEqual(item.status, 'rfc_intake')
        customer_request.refresh_from_db()
        self.assertEqual(customer_request.status, 'under_review')
        self.assertEqual(customer_request.created_item, item)

    def test_customer_request_portal_source_is_unique(self):
        customer_request = CustomerRequest.objects.create(
            project=self.project,
            title='Expose public roadmap status',
            description='Customers should see planned, in progress, and shipped status.',
        )
        item = Item.objects.create(
            mode='task',
            item_type='task.story',
            project=self.project,
            item_key=self.project.get_next_item_key(),
            title='First',
            source='customer_request_portal',
            source_id=str(customer_request.id),
            workflow_name='feature-request-rfc',
            status='rfc_intake',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.create(
                mode='task',
                item_type='task.story',
                project=self.project,
                item_key=self.project.get_next_item_key(),
                title='Duplicate',
                source='customer_request_portal',
                source_id=str(customer_request.id),
                workflow_name='feature-request-rfc',
                status='rfc_intake',
            )
        self.assertEqual(Item.objects.filter(source='customer_request_portal', source_id=str(customer_request.id)).count(), 1)
        self.assertEqual(Item.objects.get(source='customer_request_portal', source_id=str(customer_request.id)), item)


class CustomerPortalApprovalConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.project = Project.objects.create(
            org_id=1,
            name='Pulse',
            key='PLS',
            access_level='limited',
            settings={
                'customer_portal_enabled': True,
                'customer_portal_public_ref': 'pls-portal-7d9b2a6f',
                'customer_request_assignment_group': 'product',
            },
        )
        self.customer_request = CustomerRequest.objects.create(
            project=self.project,
            title='Expose public roadmap status',
            description='Customers should see planned, in progress, and shipped status.',
            category='roadmap',
            upvote_count=7,
        )
        self.user = get_user_model().objects.create_user(
            username='staff',
            email='staff@example.com',
            password='secret',
            is_staff=True,
        )

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_approval_creates_exactly_one_item(self):
        import threading

        barrier = threading.Barrier(2)
        responses = []

        def approve():
            client = APIClient()
            client.force_authenticate(user=self.user)
            barrier.wait()
            responses.append(client.post(
                f'/api/customer-portal/requests/{self.customer_request.id}/approve/',
                {'priority': 'normal'},
                format='json',
            ))

        threads = [threading.Thread(target=approve), threading.Thread(target=approve)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(response.status_code for response in responses), [200, 201])
        self.assertEqual(
            Item.objects.filter(source='customer_request_portal', source_id=str(self.customer_request.id)).count(),
            1,
        )
