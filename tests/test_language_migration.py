#!/usr/bin/env python3
"""
Test script for language migration functionality.
"""

import json

from factories import DataFactory


def test_language_factory():
    """Test the language data factory with sample data."""

    # Sample Cosmos DB language document
    sample_language = {
        "description": "French",
        "assetVersion": "africa",
        "LastUpdatedBy": "juliemollar@maternity.dk",
        "LastUpdated": 1746084949556,
        "_table": "languages",
        "id": "6a146956-9f21-206a-98cf-55db7b0a8301",
        "lastPublished": 1746084833526,
        "version": 167,
        "draftLastPublished": 1745490908843,
        "draftVersion": 165,
        "countryCode": "FR",
        "latitude": 46.2276,
        "longitude": 2.2137,
        "indicateMasterDifferences": True,
        "learningPlatform": True,
        "_rid": "gco8APGv2QZ0CAAAAAAAAA==",
        "_self": ("dbs/gco8AA==/colls/gco8APGv2QY=/docs/gco8APGv2QZ0CAAAAAAAAA==/"),
        "_etag": '"c701eeba-0000-0c00-0000-681324550000"',
        "_attachments": "attachments/",
        "_ts": 1746084949,
    }

    print("Testing language data factory...")
    print(f"Input language: {sample_language['description']}")
    print(f"Country code: {sample_language['countryCode']}")

    # Create language data using factory
    language_data = DataFactory.create_language_data(sample_language)

    print("\nGenerated LanguageData:")
    print(json.dumps(language_data.to_dict(), indent=2))

    # Verify mappings
    assert language_data.language_name == "French"
    assert language_data.region == "africa"
    assert language_data.country_code == "FR"
    assert language_data.latitude == 46.2276
    assert language_data.longitude == 2.2137

    print("\n✅ Language factory test passed!")


def test_module_factory():
    """Test the module data factory with sample data."""

    # Sample Cosmos DB module document
    sample_module = {
        "actionCards": [],
        "procedures": ["proc1", "proc2"],
        "videos": ["vid1"],
        "drugs": ["drug1"],
        "keyLearningPoints": [
            "test-version_1498639439941",
            "septic-abortion-_1504773307717",
        ],
        "key": "post-abortion-care_1487676484604",
        "description": "Post Abortion Care",
        "icon": "/icon/module/post_abortion_care",
        "langId": "",
        "LastUpdatedBy": "stine@maternityworldwide.dk",
        "LastUpdated": 1504894565858,
        "_table": "modules",
        "id": "66d5fc29-e846-5e4f-b0d3-ae1ebe8ff510",
    }

    print("\nTesting module data factory...")
    print(f"Input module: {sample_module['description']}")

    # Create module data using factory
    module_data = DataFactory.create_module_data(sample_module)

    print("\nGenerated ModuleData:")
    print(json.dumps(module_data.to_dict(), indent=2))

    # Verify mappings
    assert module_data.title == "Post Abortion Care"
    assert module_data.description == "Post Abortion Care"
    assert module_data.created_by == "stine@maternityworldwide.dk"
    # Videos are now processed as assets, empty when failed
    assert module_data.videos == []
    assert module_data.action_cards == []
    assert module_data.practical_procedures == ["proc1", "proc2"]
    assert module_data.drugs == ["drug1"]

    print("\n✅ Module factory test passed!")


def test_asset_caching():
    """Test that asset caching works correctly."""
    print("\nTesting asset caching...")

    # Clear any existing cache
    DataFactory._asset_cache.clear()

    # Test with a mock icon path (will fail but should cache the failure)
    test_icon_path = "/test/icon/path"

    # First call - should attempt download/upload
    result1 = DataFactory._download_and_upload_icon(test_icon_path)
    print(f"First call result: {result1}")

    # Second call - should use cache (even though first failed)
    result2 = DataFactory._download_and_upload_icon(test_icon_path)
    print(f"Second call result: {result2}")

    # Check that cache contains the entry (now uses full URL as key)
    expected_url = (
        "https://sdacms.blob.core.windows.net/content/assets/"
        "videos/test/icon/path.png"
    )
    assert expected_url in DataFactory._asset_cache
    print(f"Cache contains: {DataFactory._asset_cache}")

    # Test cache statistics
    stats = DataFactory.get_asset_cache_stats()
    print(f"Cache stats: {stats}")
    assert stats["total_cached"] == 1
    assert stats["failed_attempts"] == 1
    assert stats["successful_uploads"] == 0

    print("\n✅ Asset caching test passed!")


def test_resource_factory():
    """Test the resource data factory with sample data."""

    # Sample Cosmos DB resource documents for different types
    test_cases = [
        {
            "doc": {
                "description": "Test Video",
                "content": "Video content here",
                "language_id": "lang123",
                "LastUpdatedBy": "test_user",
                "_table": "videos",
            },
            "table_type": "videos",
            "expected_content_type": "video",
        },
        {
            "doc": {
                "description": "Test Action Card",
                "content": "Action card content",
                "language_id": "lang456",
                "LastUpdatedBy": "test_user2",
                "_table": "actionCards",
            },
            "table_type": "actionCards",
            "expected_content_type": "action-card",
        },
        {
            "doc": {
                "description": "Test Drug Info",
                "content": "Drug information",
                "language_id": "lang789",
                "LastUpdatedBy": "test_user3",
                "_table": "drugs",
            },
            "table_type": "drugs",
            "expected_content_type": "drug",
        },
    ]

    for i, test_case in enumerate(test_cases, 1):
        print(
            f"\nTesting resource factory - Case {i}: "
            f"{test_case['expected_content_type']}"
        )

        # Create resource data using factory
        resource_data = DataFactory.create_resource_data(
            test_case["doc"], test_case["table_type"]
        )

        print("Generated ResourcePostRequestData:")
        print(f"Title: {resource_data.title}")
        print(f"Content Type: {resource_data.content_type}")
        print(f"Language ID: {resource_data.language_id}")
        print(f"Region: {resource_data.region}")

        # Verify mappings
        assert resource_data.title == test_case["doc"]["description"]
        assert resource_data.content_type == test_case["expected_content_type"]
        assert resource_data.language_id == test_case["doc"]["language_id"]
        assert resource_data.region == "africa"

        print(f"✅ Resource factory test case {i} passed!")

    print("✅ Resource factory tests passed!")


def test_action_card_factory():
    """Test the action-card data factory with mock Cosmos DB client."""
    from unittest.mock import Mock

    print("\nTesting action-card data factory...")

    # Create mock Cosmos DB client and container
    mock_cosmos_client = Mock()
    mock_container = Mock()

    # Mock successful query result
    mock_action_card_doc = {
        "key": "test_action_card_key",
        "description": "Test Action Card",
        "content": "Action card content here",
        "_table": "action-cards",
        "id": "test-action-card-id",
    }
    mock_container.query_items.return_value = [mock_action_card_doc]

    # Test successful fetch
    result = DataFactory.get_action_card_data(
        mock_cosmos_client, mock_container, "test_action_card_key"
    )

    assert result is not None
    assert result["key"] == "test_action_card_key"
    assert result["description"] == "Test Action Card"
    print("✅ Action-card fetch test passed!")

    # Test with empty key
    result_empty = DataFactory.get_action_card_data(
        mock_cosmos_client, mock_container, ""
    )
    assert result_empty is None
    print("✅ Empty key test passed!")

    # Test with no results
    mock_container.query_items.return_value = []
    result_none = DataFactory.get_action_card_data(
        mock_cosmos_client, mock_container, "nonexistent_key"
    )
    assert result_none is None
    print("✅ No results test passed!")

    # Test with exception
    mock_container.query_items.side_effect = Exception("Query failed")
    result_error = DataFactory.get_action_card_data(
        mock_cosmos_client, mock_container, "error_key"
    )
    assert result_error is None
    print("✅ Exception handling test passed!")


if __name__ == "__main__":
    test_language_factory()
    test_module_factory()
    test_asset_caching()
    test_resource_factory()
    test_action_card_factory()
